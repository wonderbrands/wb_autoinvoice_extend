# -*- coding: utf-8 -*-
from odoo import http, _, fields
from odoo.http import request
from odoo.addons.q_l10n_mx_autoinvoice.controllers.main import Autoinvoice
from datetime import date, timedelta
from odoo.exceptions import UserError
import re

import logging
_logger = logging.getLogger(__name__)


# ----------------------------------------------------------
#Normalización de textos
# ----------------------------------------------------------
import unicodedata

def normalize_text(value):
    """Convierte a MAYÚSCULAS y elimina acentos"""
    if not value or not isinstance(value, str):
        return value
    nfkd = unicodedata.normalize('NFKD', value)
    no_accent = ''.join([c for c in nfkd if not unicodedata.combining(c)])
    return no_accent.upper()


def normalize_values(values: dict):
    """Normaliza todos los valores string de un dict"""
    return {k: normalize_text(v) if isinstance(v, str) else v for k, v in values.items()}


class AutoinvoiceExtended(Autoinvoice):  # Heredo de la clase Autoinvoice original

    @http.route('/q_l10n_mx_autoinvoice/order', type='json', auth='public', website=True, csrf=False)
    def autoinvoice_order(self, number_order=False, amount_total=0):
        user_root = request.env.ref('base.user_root')
        res_config_settings = request.env['res.config.settings'].sudo().with_user(user_root).get_values()

        order = request.env['sale.order'].sudo().with_user(user_root).search([
            ('name', '=', number_order),
            ('company_id', '=', request.env.user.company_id.id)
        ])

        if not order and res_config_settings.get('autoinvoice_mercadolibre'):
            order = request.env['sale.order'].sudo().with_user(user_root).search([
                '|',
                ('name', '=', f"ML {number_order}"),
                ('meli_order_id', '=', number_order),
                ('company_id', '=', request.env.user.company_id.id)
            ])

        if not order:
            return {'error': _('No se encontró la orden de venta.')}

        # -----------------------------------------------------------------------
        # Validación por fechas
        today = date.today()
        order_date = order.date_order.date()
        days_diff = (today - order_date).days

        if order_date.year < today.year:
            if days_diff > 180 or today.month > 3:
                return {'error': _('La orden es del año anterior. Solo puede facturarse si tiene menos de 180 días y si estamos antes del 31 de marzo del año actual.')}
        elif days_diff > 180:
            return {'error': _('La orden excede los 180 días permitidos para facturación.')}

        # -----------------------------------------------------------------------
        # Validar monto
        if abs(float(order.amount_total) - float(amount_total)) > float(res_config_settings['autoinvoice_tolerance']):
            return {'error': _('Not exist order with these records.')}

        if order.state not in ('sale', 'done'):
            return {'error': _('The order is not confirmed.')}

        # -----------------------------------------------------------------------
        # Ya facturada a cliente final
        already_factured = order.invoice_ids.filtered(
            lambda inv: inv.move_type == 'out_invoice' and
                        inv.partner_id.vat != 'XAXX010101000' and
                        inv.state == 'posted'
        )
        if already_factured:
            return {'error': _(f"La orden ya fue facturada a cliente final.")} # RFC: {already_factured[0].partner_id.vat}.")}

        # -----------------------------------------------------------------------
        # Factura global
        global_invoice = order.invoice_ids.filtered(
            lambda inv: inv.move_type == 'out_invoice' and
                        inv.partner_id.vat == 'XAXX010101000' and
                        inv.state == 'posted'
        )

        # NC creada desde el autofacturador
        nc_autoinvoice = order.invoice_ids.filtered(
            lambda inv: inv.move_type == 'out_refund' and
                        inv.from_autoinvoice and
                        inv.state == 'posted'
        )

        # Verificar si existe factura final después de una NC
        factura_final = order.invoice_ids.filtered(
            lambda inv: inv.move_type == 'out_invoice' and
                        inv.partner_id.vat != 'XAXX010101000' and
                        inv.state == 'posted'
        )

        # -----------------------------------------------------------------------
        if nc_autoinvoice and not factura_final:
            # Se permite continuar, fue una refactura anterior inconclusa
            rinv_incomplete = True

        elif order.invoice_ids.filtered(lambda inv: inv.move_type == 'out_refund' and inv.state == 'posted'):
            return {'error': _('Ya existe una nota de crédito asociada a esta orden.')}
        else:
            rinv_incomplete = False

            # -----------------------------------------------------------------------
            # CAMBIO CLAVE: La creación de la NC EN BORRADOR se hace aquí, al inicio,
            # para "desbloquear" la orden de venta y permitir la creación de la nueva factura.
            # -----------------------------------------------------------------------
            if global_invoice and not rinv_incomplete:
                try:
                    # Llamamos a la función que SÓLO crea el borrador
                    credit_note_draft = order.sudo()._create_draft_credit_note_for_autoinvoice(global_invoice[0])

                    # Guardamos el ID de la NC en borrador en la sesión del usuario.
                    # Esto nos permitirá recuperarla en el último paso para publicarla o eliminarla.
                    request.session['autoinvoice_draft_nc_id'] = credit_note_draft.id
                    #print(f"NC en borrador {credit_note_draft.name} (ID: {credit_note_draft.id}) creada y guardada en sesión.")
                except Exception as e:
                    # Si falla la creación del borrador de la NC, detenemos el proceso.
                    ##print("No se pudo crear la NC en borrador. Razón: %s", str(e))
                    return {'error': str(e.args[0]) if isinstance(e, UserError) else str(e)}

            # Imposibilita al cliente crear factura si aun no se ha entregado al menos una unidad de alun SKU
            # Si solo tiene el envio hecho 'C-ENVIO', NO DEJA FACTURAR
            SHIPPING_CODE = ['C-ENVIO']
            non_shipping_lines = order.order_line.filtered(lambda l: not (
                    (l.product_id.default_code and l.product_id.default_code.upper() in SHIPPING_CODE) or
                    (l.product_id.name and l.product_id.name.upper() in SHIPPING_CODE)
            ))
            delivered_non_shipping = any([l.qty_delivered > 0 for l in non_shipping_lines])
            if not delivered_non_shipping:
                # Si fallamos aquí, debemos asegurarnos de limpiar la NC en borrador si se creó
                draft_nc_id = request.session.pop('autoinvoice_draft_nc_id', None)
                if draft_nc_id:
                    request.env['account.move'].sudo().browse(draft_nc_id).unlink()
                    #print(f"Rollback: NC en borrador {draft_nc_id} eliminada por fallo en validación de entrega.")
                return {'error': _('No se puede facturar: Aun no hay artículos a facturar para esta orden')}

            # El flujo ahora puede continuar al formulario de dirección
            template = request.env['ir.ui.view']._render_template('q_l10n_mx_autoinvoice.address', {
                'country_id': request.env.ref('base.mx'),
            })
            return {
                'order_id': order.id,
                'template': template,
            }


    @http.route('/q_l10n_mx_autoinvoice/select_address', type='json', auth='public', website=True, csrf=False)
    def autoinvoice_select_address(self, order_id, partner_id):
        # La lógica de crear la factura en borrador funcionará
        # porque la NC en borrador (creada en el paso anterior) ya "liberó" la orden de venta.
        user_root = request.env.ref('base.user_root')
        try:
            order = request.env['sale.order'].sudo().with_user(user_root).search([
                ('id', '=', int(order_id)),
                ('company_id', '=', request.website.company_id.id)
            ])
            order.write({
                'partner_invoice_id': int(partner_id),
            })

            # CREAR FACTURA NUEVA DESDE CERO
            invoice = order._create_invoices()
            invoice.write({
                'partner_id': int(partner_id),
                'ref': f"Factura cliente de {order.name}",
            })

            template = request.env['ir.ui.view']._render_template(
                'q_l10n_mx_autoinvoice.additional_information')

            message = {
                'body': f"<p>Factura creada automáticamente a petición del cliente de la orden <b>{order.name}</b>.</p>",
                'message_type': 'notification',
                'subtype_id': request.env.ref('mail.mt_note').id,
            }

            invoice.message_post(**message)

            return {
                'invoice_id': invoice.id,
                'template': template,
            }
        except Exception as error:
            return {'error': str(error)}

    # ----------------------------------------------------------
    #Normalización en ADD_ADDRESS
    # CAMBIO: Se reescribe 'autoinvoice_add_address' para asegurar que el país (country_id)
    # siempre se establezca al crear o buscar un partner, solucionando el error de validación de RFC.
    # Guarda un "backup" de los datos originales en la sesión.
    @http.route('/q_l10n_mx_autoinvoice/add_address', type='json', auth='public', website=True, csrf=False)
    def autoinvoice_add_address(self, name=False, vat=False, zipcode=False, **kwargs):
        user_root = request.env.ref('base.user_root')
        try:
            if 'autoinvoice_partner_backup' in request.session:
                del request.session['autoinvoice_partner_backup']
            if not vat:
                return {'error': 'El campo RFC es obligatorio.'}

            # Normalizamos y limpiamos el RFC para una búsqueda fiable.
            search_vat = normalize_text(vat).strip()

            # Buscamos un partner cuyo RFC sea IGUAL (case-insensitive) al proporcionado.
            # Además, nos aseguramos de que no sea un contacto de una compañía (is_company=False o company_type='person')
            # Esto evita conflictos si una compañía tiene el mismo RFC.
            partner = request.env['res.partner'].sudo().search([
                ('vat', '=ilike', search_vat),
                '|', ('is_company', '=', False), ('company_type', '=', 'person')
            ], limit=1)

            vals_from_form = normalize_values({'name': name, 'zipcode': zipcode})
            values_to_write = {
                'name': vals_from_form['name'],
                'zip': vals_from_form['zipcode'],
                'country_id': request.env.ref('base.mx').id,
            }

            if partner:
                #print(f"Partner {partner.name} (ID: {partner.id}) encontrado. Guardando backup y actualizando.")
                backup_data = {'partner_id': partner.id,
                               'original_values': {'name': partner.name, 'zip': partner.zip,
                                                   'l10n_mx_edi_fiscal_regime': partner.l10n_mx_edi_fiscal_regime}}
                request.session['autoinvoice_partner_backup'] = backup_data
                partner.sudo().with_user(user_root).write(values_to_write)
            else:
                #print(f"No se encontró partner. Creando uno nuevo con RFC: {search_vat}")
                create_values = values_to_write.copy()
                create_values.update({'vat': search_vat, 'type': 'invoice'})
                partner = request.env['res.partner'].sudo().with_user(user_root).create(create_values)
                request.session['autoinvoice_partner_backup'] = {'new_partner_id': partner.id}

            return {'partner_id': partner.id}
        except Exception as error:
            _logger.error(f"Error en autoinvoice_add_address: {str(error)}")
            return {'error': str(error.args[0]) if isinstance(error, UserError) else 'Ocurrió un error al procesar sus datos.'}

    # ----------------------------------------------------------
    #Normalización en INFORMATION
    # CAMBIO: Sobrescribimos 'autoinvoice_information' para tomar control total
    # y evitar que el método original del módulo base publique la factura prematuramente.
    # ----------------------------------------------------------
    @http.route('/q_l10n_mx_autoinvoice/information', type='json', auth='public', website=True, csrf=False)
    def autoinvoice_information(self, invoice_id, fiscal_regime=False, use_of_cfdi=False, payment_method=False):
        # Normalización de texto
        fiscal_regime = normalize_text(fiscal_regime)
        use_of_cfdi = normalize_text(use_of_cfdi)
        payment_method = normalize_text(payment_method)

        try:
            invoice = request.env['account.move'].sudo().browse(int(invoice_id))

            # Replicamos la lógica simple de solo GUARDAR los datos en la factura en borrador.
            # Ya no llamamos a super(), evitando así el action_post() prematuro.
            payment_method_id = request.env['l10n_mx_edi.payment.method'].sudo().search(
                [('code', '=', payment_method)], limit=1).id

            # Escribimos los valores en la factura y el partner asociados
            invoice.write({
                'l10n_mx_edi_usage': use_of_cfdi,
                'l10n_mx_edi_payment_method_id': payment_method_id,
                'l10n_mx_edi_payment_policy': 'PUE',  # Asumimos PUE para autofactura
            })
            invoice.partner_id.write({
                'l10n_mx_edi_fiscal_regime': fiscal_regime,
            })

            #print(f"Información fiscal guardada en la factura borrador {invoice.name}. La factura NO ha sido publicada.")

            # Devolvemos éxito para que el JavaScript del módulo base proceda
            # a cambiar el botón de "Check" a "Timbrar".
            return {'success': _('Information updated.')}

        except Exception as e:
            _logger.error(f"Error en el paso intermedio 'autoinvoice_information': {str(e)}")
            return {'error': 'Ocurrió un error al guardar la información fiscal.'}

    # ----------------------------------------------------------
    #Normalización en VALIDATE_INVOICE
    # ----------------------------------------------------------
    # CAMBIO: Se sobrescribe 'validate_invoice' para implementar la transacción "Todo o Nada".
    # CAMBIO FINAL: Se reestructura 'validate_invoice' para "envolver" la llamada a super()
    # en lugar de reemplazarla. Esto asegura que la lógica de timbrado original se ejecute
    # dentro de nuestro entorno transaccional seguro.
    @http.route('/q_l10n_mx_autoinvoice/validate_invoice', type='json', auth='public', website=True, csrf=False)
    def autoinvoice_validate_invoice(self, invoice_id, fiscal_regime=False, use_of_cfdi=False,
                                     payment_method=False):
        # --- PREPARACIÓN: Obtenemos todos los registros necesarios del entorno y la sesión ---
        user_root = request.env.ref('base.user_root')
        env = request.env(user=user_root.id)  # Usamos un entorno de superusuario para consistencia

        customer_invoice_draft = env['account.move'].browse(int(invoice_id))
        order = customer_invoice_draft.line_ids.sale_line_ids.order_id

        draft_nc_id = request.session.get('autoinvoice_draft_nc_id')
        credit_note_draft = env['account.move'].browse(draft_nc_id) if draft_nc_id else None
        partner_backup = request.session.get('autoinvoice_partner_backup')
        attempt_count = request.session.get('autoinvoice_attempt_count', 1)

        #_logger.info(f"Iniciando validación final para la orden {order.name} - Intento #{attempt_count}")

        try:
            # -----------------------------------------------------------------
            # Lógica de Preparación
            # Se prepara la factura del cliente con los datos finales antes de llamar a super().
            # -----------------------------------------------------------------
            #_logger.info("Aplicando datos fiscales a la factura borrador...")
            payment_method_id = env['l10n_mx_edi.payment.method'].search([('code', '=', payment_method)], limit=1).id
            customer_invoice_draft.write({
                'l10n_mx_edi_usage': use_of_cfdi,
                'l10n_mx_edi_payment_method_id': payment_method_id,
            })
            customer_invoice_draft.partner_id.write({'l10n_mx_edi_fiscal_regime': normalize_text(fiscal_regime)})

            # -----------------------------------------------------------------
            # Llamamos al método original del módulo base
            # -----------------------------------------------------------------
            #_logger.info("Llamando a la lógica de timbrado original (super)...")
            # El módulo base hace el trabajo pesado de publicar y timbrar
            result_from_super = super(AutoinvoiceExtended, self).autoinvoice_validate_invoice(
                invoice_id, fiscal_regime, use_of_cfdi, payment_method
            )
            #_logger.info("Lógica de timbrado original (super) completada.")

            # -----------------------------------------------------------------
            # Revición del resultado y lógica de Finalización
            # -----------------------------------------------------------------
            customer_invoice_draft.invalidate_cache(['l10n_mx_edi_cfdi_uuid'])
            if customer_invoice_draft.l10n_mx_edi_cfdi_uuid:
                # ÉXITO: El 'super' funcionó y timbró la factura.
                _logger.info(f"Éxito. La factura {customer_invoice_draft.name} tiene UUID. Procesando NC...")
                if credit_note_draft and credit_note_draft.exists():
                    global_invoice = order.invoice_ids.filtered(lambda
                                                                    i: i.move_type == 'out_invoice' and i.partner_id.vat == 'XAXX010101000' and i.state == 'posted')
                    order._commit_credit_note_for_autoinvoice(credit_note_draft, global_invoice)

                # Limpiamos sesión y devolvemos el resultado exitoso que nos dio super().
                if 'autoinvoice_draft_nc_id' in request.session: del request.session['autoinvoice_draft_nc_id']
                if 'autoinvoice_attempt_count' in request.session: del request.session['autoinvoice_attempt_count']
                if 'autoinvoice_partner_backup' in request.session: del request.session[
                    'autoinvoice_partner_backup']
                return result_from_super
            else:
                # FALLO: El 'super' falló silenciosamente o el PAC rechazó.
                request.session['autoinvoice_attempt_count'] = attempt_count + 1
                edi_doc = customer_invoice_draft.edi_document_ids.filtered(lambda d: d.state == 'to_send_failed')
                # Extraemos el error del resultado de super() si existe, si no, del documento EDI.
                raw_error_from_pac = (result_from_super.get('error') if isinstance(result_from_super, dict)
                                      else edi_doc and edi_doc[0].error or 'El PAC rechazó el documento.')

                # LLAMAMOS A NUESTRA FUNCIÓN DE LIMPIEZA
                error_from_pac_clean = self._clean_pac_error_message(raw_error_from_pac)

                error_message = f"Intento #{attempt_count}: {error_from_pac_clean}  ¡Verifique sus datos!"
                if attempt_count >= 2:
                    error_message += " Si el problema persiste, contacte a Soporte al Cliente."
                raise UserError(error_message)

        except Exception as e:
            # -----------------------------------------------------------------
            # FASE DE ERROR: ROLLBACK COMPLETO Y SEGURO USANDO .SUDO()
            # -----------------------------------------------------------------
            error_message_to_show = str(e.args[0]) if isinstance(e,
                                                                 UserError) else 'Ocurrió un error inesperado de sistema.'
            _logger.error(f"FALLO en la transacción de autofactura: {error_message_to_show}", exc_info=False)

            try:
                _logger.info("Iniciando rollback completo...")

                # 1. Rollback del Partner (cliente)
                if partner_backup:
                    if 'original_values' in partner_backup:
                        partner_to_restore = request.env['res.partner'].sudo().browse(partner_backup['partner_id'])
                        if partner_to_restore.exists():
                            partner_to_restore.write(partner_backup['original_values'])
                            _logger.info(f"Rollback: Datos del partner {partner_to_restore.name} restaurados.")
                    elif 'new_partner_id' in partner_backup:
                        request.env['res.partner'].sudo().browse(partner_backup['new_partner_id']).unlink()
                        _logger.info(f"Rollback: Partner nuevo (ID: {partner_backup['new_partner_id']}) eliminado.")

                # 2. Preparamos el "lote" de documentos a borrar
                records_to_delete = request.env['account.move']
                if credit_note_draft and credit_note_draft.exists():
                    records_to_delete |= credit_note_draft
                if customer_invoice_draft and customer_invoice_draft.exists():
                    if customer_invoice_draft.state == 'posted':
                        customer_invoice_draft.button_draft()
                    records_to_delete |= customer_invoice_draft

                # 3. Borramos todos los documentos en una SOLA operación usando sudo().
                if records_to_delete:
                    records_to_delete.sudo().unlink()
                    _logger.info(f"Rollback: Documentos (IDs: {records_to_delete.ids}) eliminados en lote.")

            except Exception as rollback_e:
                # Si incluso este rollback simple falla, lo registramos pero continuamos.
                _logger.critical("¡ERROR CRÍTICO DURANTE EL ROLLBACK!: %s", str(rollback_e))

            finally:
                # ESTE BLOQUE 'finally' SIEMPRE SE EJECUTA
                _logger.info("Finalizando rollback y preparando respuesta al navegador.")

                # Limpieza de sesión
                if 'autoinvoice_draft_nc_id' in request.session: del request.session['autoinvoice_draft_nc_id']
                if 'autoinvoice_partner_backup' in request.session: del request.session[
                    'autoinvoice_partner_backup']

                # Respuesta garantizada al frontend
                return {'restart_flow': True, 'error': error_message_to_show}

    def _clean_pac_error_message(self, raw_error):
        """
        Filtra un mensaje de error del PAC para extraer solo el texto relevante,
        eliminando CUALQUIER etiqueta HTML que encuentre.
        """
        if not isinstance(raw_error, str):
            return raw_error

        mensaje = raw_error
        # Aislar el mensaje principal, si es posible.
        if '<li>Mensaje: ' in mensaje:
            try:
                mensaje = mensaje.split('<li>Mensaje: ')[1]
            except IndexError:
                # Si el split falla por alguna razón, usamos el string original pero continuamos
                pass

        # Usamos una expresión regular para encontrar y eliminar todas las etiquetas HTML.
        # La expresión r'<.*?>' busca cualquier cosa que empiece con '<' y termine con '>'.
        patron_html = re.compile(r'<.*?>')
        mensaje_sin_html = re.sub(patron_html, '', mensaje)

        # Limpiamos espacios en blanco y devolvemos el texto puro.
        return mensaje_sin_html.strip()