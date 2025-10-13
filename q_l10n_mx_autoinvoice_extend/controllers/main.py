# -*- coding: utf-8 -*-
from odoo import http, _, fields
from odoo.http import request
from odoo.addons.q_l10n_mx_autoinvoice.controllers.main import Autoinvoice
from datetime import date, timedelta
from odoo.exceptions import UserError

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
            return {
                'error': _(f"La orden ya fue facturada a cliente final. RFC: {already_factured[0].partner_id.vat}.")}

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
                    #_logger.info(f"NC en borrador {credit_note_draft.name} (ID: {credit_note_draft.id}) creada y guardada en sesión.")
                except Exception as e:
                    # Si falla la creación del borrador de la NC, detenemos el proceso.
                    #_logger.error("No se pudo crear la NC en borrador. Razón: %s", str(e))
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
                    #_logger.info(f"Rollback: NC en borrador {draft_nc_id} eliminada por fallo en validación de entrega.")
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
    # ----------------------------------------------------------
    @http.route('/q_l10n_mx_autoinvoice/add_address', type='json', auth='public', website=True, csrf=False)
    def autoinvoice_add_address(self, name=False, vat=False, zipcode=False, email=False, **kwargs):
        # Usamos **kwargs para ignorar de forma segura los campos que ya no usamos (street_name, city, etc.)
        user_root = request.env.ref('base.user_root')

        try:
            # Normalizamos los valores que sí recibimos.
            vals = normalize_values({
                'name': name,
                'vat': vat,
                'zipcode': zipcode,
                'email': email,
            })

            # Buscamos si ya existe un partner con esa combinación de datos
            domain = [('name', 'ilike', vals['name']), ('vat', '=', vals['vat'])]
            if vals['vat'] != 'XAXX010101000':
                domain.append(('zip', '=', vals['zipcode']))

            partner = request.env['res.partner'].sudo().search(domain, limit=1)

            # Si no existe el partner, lo creamos con los datos mínimos y el país correcto.
            if not partner:
                #_logger.info(f"No se encontró el partner. Creando uno nuevo con RFC: {vals['vat']}")
                partner_values = {
                    'type': 'invoice',
                    'name': vals['name'],
                    'vat': vals['vat'],
                    'zip': vals['zipcode'],
                    'email': vals['email'],
                    # LÍNEA CLAVE: Se asigna México como país por defecto.
                    'country_id': request.env.ref('base.mx').id,
                }
                partner = request.env['res.partner'].sudo().with_user(user_root).create(partner_values)

            # Devolvemos el ID del partner encontrado o recién creado.
            return {
                'partner_id': partner.id,
            }
        except Exception as error:
            #_logger.error("Error en autoinvoice_add_address: %s", str(error))
            # Devolvemos un error claro al frontend
            return {'error': str(error.args[0]) if isinstance(error,
                                                              UserError) else 'Ocurrió un error al procesar sus datos.'}

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

            #_logger.info(f"Información fiscal guardada en la factura borrador {invoice.name}. La factura NO ha sido publicada.")

            # Devolvemos éxito para que el JavaScript del módulo base proceda
            # a cambiar el botón de "Check" a "Timbrar".
            return {'success': _('Information updated.')}

        except Exception as e:
            #_logger.error("Error en el paso intermedio 'autoinvoice_information': %s", str(e))
            return {'error': 'Ocurrió un error al guardar la información fiscal.'}

    # ----------------------------------------------------------
    #Normalización en VALIDATE_INVOICE
    # ----------------------------------------------------------
    # CAMBIO: Se sobrescribe 'validate_invoice' para implementar la transacción "Todo o Nada".
    # CAMBIO FINAL: Lógica completa de "dos intentos" con "reinicio amigable" para cualquier tipo de error.
    @http.route('/q_l10n_mx_autoinvoice/validate_invoice', type='json', auth='public', website=True, csrf=False)
    def autoinvoice_validate_invoice(self, invoice_id, fiscal_regime=False, use_of_cfdi=False,
                                     payment_method=False):
        # --- PREPARACIÓN: Obtenemos todos los registros necesarios ---
        user_root = request.env.ref('base.user_root')
        customer_invoice_draft = request.env['account.move'].sudo().browse(int(invoice_id))
        order = customer_invoice_draft.line_ids.sale_line_ids.order_id

        # Recuperamos el ID de la NC en borrador y el contador de intentos desde la sesión del usuario.
        draft_nc_id = request.session.get('autoinvoice_draft_nc_id')
        credit_note_draft = request.env['account.move'].sudo().browse(draft_nc_id) if draft_nc_id else None
        attempt_count = request.session.get('autoinvoice_attempt_count', 1)

        #_logger.info(f"Procesando autofactura para la orden {order.name} - Intento #{attempt_count}")

        try:
            # -----------------------------------------------------------------
            # FASE 1: PREPARAR Y PUBLICAR LA FACTURA DEL CLIENTE
            # -----------------------------------------------------------------
            #_logger.info(f"Fase Final: Intentando publicar y timbrar la factura del cliente {customer_invoice_draft.name}...")
            payment_method_id = request.env['l10n_mx_edi.payment.method'].sudo().search(
                [('code', '=', payment_method)],
                limit=1).id
            now = fields.Datetime.now()

            # Escribimos los datos fiscales finales y forzamos la fecha actual para evitar errores del PAC.
            customer_invoice_draft.write({
                'invoice_date': now.date(),
                'date': now.date(),
                'l10n_mx_edi_usage': use_of_cfdi,
                'l10n_mx_edi_payment_method_id': payment_method_id,
                'l10n_mx_edi_payment_policy': 'PUE',
            })
            customer_invoice_draft.partner_id.write({'l10n_mx_edi_fiscal_regime': normalize_text(fiscal_regime)})

            # Publicamos el asiento. Esto también dispara el intento de timbrado EDI.
            customer_invoice_draft.action_post()

            # -----------------------------------------------------------------
            # FASE 2: VERIFICACIÓN EXPLÍCITA DEL RESULTADO DEL TIMBRADO
            # -----------------------------------------------------------------
            customer_invoice_draft.invalidate_cache(['l10n_mx_edi_cfdi_uuid'])

            # La prueba definitiva: ¿La factura tiene un UUID del SAT?
            if customer_invoice_draft.l10n_mx_edi_cfdi_uuid:
                # -----------------------------------------------------------------
                # FASE 3: ÉXITO - "COMMIT" FINAL DE LA NOTA DE CRÉDITO
                # -----------------------------------------------------------------
                #_logger.info(f"Éxito. La factura {customer_invoice_draft.name} tiene UUID. Procesando NC...")
                if credit_note_draft and credit_note_draft.exists():
                    global_invoice = order.invoice_ids.filtered(
                        lambda
                            i: i.move_type == 'out_invoice' and i.partner_id.vat == 'XAXX010101000' and i.state == 'posted'
                    )
                    order.sudo().with_user(user_root)._commit_credit_note_for_autoinvoice(credit_note_draft,
                                                                                          global_invoice)

                customer_invoice_draft.write({'from_autoinvoice': True})

                # Limpiamos las variables de la sesión al terminar con éxito
                if 'autoinvoice_draft_nc_id' in request.session: del request.session['autoinvoice_draft_nc_id']
                if 'autoinvoice_attempt_count' in request.session: del request.session['autoinvoice_attempt_count']

                # Devolvemos la plantilla de descarga al frontend
                template = request.env['ir.ui.view'].sudo()._render_template('q_l10n_mx_autoinvoice.download',
                                                                             {
                                                                                 'invoice_id': customer_invoice_draft.id})
                return {'template': template}
            else:
                # --- FALLO DE TIMBRADO ---
                #_logger.warning(f"Intento #{attempt_count} fallido: No se generó UUID para la factura {customer_invoice_draft.name}.")
                edi_document = customer_invoice_draft.edi_document_ids.filtered(
                    lambda d: d.state == 'to_send_failed')
                error_from_pac = edi_document and edi_document[
                    0].error or 'El PAC rechazó el documento. Verifique sus datos.'

                # Devolvemos la factura a borrador.
                if customer_invoice_draft.state == 'posted':
                    customer_invoice_draft.button_draft()

                if attempt_count < 2:
                    # PRIMER FALLO: Rollback completo y reinicio amigable.
                    #_logger.info("Primer intento fallido. Realizando rollback completo y pidiendo reinicio del flujo.")
                    request.session['autoinvoice_attempt_count'] = attempt_count + 1

                    if credit_note_draft and credit_note_draft.exists():
                        credit_note_draft.sudo().unlink()
                    if customer_invoice_draft and customer_invoice_draft.exists():
                        customer_invoice_draft.sudo().unlink()

                    if 'autoinvoice_draft_nc_id' in request.session: del request.session['autoinvoice_draft_nc_id']

                    return {'restart_flow': True, 'error': error_from_pac}
                else:
                    # SEGUNDO FALLO: Dejamos todo en borrador y forzamos el reinicio.
                    #_logger.error("Segundo intento fallido. Forzando reinicio y mostrando mensaje final.")
                    final_message = ("No fue posible validar sus datos fiscales después de dos intentos. "
                                     "Por favor, contacte a Soporte al Cliente con su número de orden para recibir ayuda. "
                                     f"Error del SAT: {error_from_pac}")

                    # Limpiamos la sesión. Los documentos se quedan en borrador para revisión.
                    if 'autoinvoice_draft_nc_id' in request.session: del request.session['autoinvoice_draft_nc_id']
                    if 'autoinvoice_attempt_count' in request.session: del request.session[
                        'autoinvoice_attempt_count']

                    return {'restart_flow': True, 'error': final_message}

        except Exception as e:
            # -----------------------------------------------------------------
            # CAPTURA DE ERRORES CRÍTICOS (ROLLBACK TOTAL)
            # -----------------------------------------------------------------
            error_message = str(e.args[0]) if isinstance(e,
                                                         UserError) else 'Ocurrió un error inesperado de sistema.'
            #_logger.error(f"FALLO CRÍTICO en la transacción: {error_message}", exc_info=True)

            if credit_note_draft and credit_note_draft.exists():
                credit_note_draft.sudo().unlink()

            if customer_invoice_draft and customer_invoice_draft.state == 'posted':
                customer_invoice_draft.button_draft()

            # Limpiamos todas nuestras variables de sesión.
            if 'autoinvoice_draft_nc_id' in request.session: del request.session['autoinvoice_draft_nc_id']
            if 'autoinvoice_attempt_count' in request.session: del request.session['autoinvoice_attempt_count']

            return {'restart_flow': True, 'error': error_message}