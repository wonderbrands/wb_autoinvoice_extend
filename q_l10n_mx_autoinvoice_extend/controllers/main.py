# -*- coding: utf-8 -*-
from odoo import http, _, fields
from odoo.http import request
from odoo.addons.q_l10n_mx_autoinvoice.controllers.main import Autoinvoice
from datetime import date, timedelta

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
        # Crear la nota de crédito si existe la global
        if global_invoice and not rinv_incomplete:
            order._reprocess_from_global_invoice(global_invoice[0])

        # -----------------------------------------------------------------------
        # Mostrar formulario de dirección
        template = request.env['ir.ui.view']._render_template('q_l10n_mx_autoinvoice.address', {
            'country_id': request.env.ref('base.mx'),
        })


        # ----------------------------------------------------------
        # Cambio 11-sep-2025
        # Imposibilita al cliente crear factura si aun no se ha entregado al menos una unidad de alun SKU
        # Si solo tiene el envio hecho 'C-ENVIO', NO DEJA FACTURAR

        SHIPPING_CODE = ['C-ENVIO']

        # Filtramos las líneas que no sean envío
        non_shipping_lines = order.order_line.filtered(lambda l: not (
                (l.product_id.default_code and l.product_id.default_code.upper() in SHIPPING_CODE) or
                (l.product_id.name and l.product_id.name.upper() in SHIPPING_CODE)
        ))

        # Comprobamos si hay alguna linea no envío con qty entregada > 0
        delivered_non_shipping = any([l.qty_delivered > 0 for l in non_shipping_lines])
        if not delivered_non_shipping:
            return {'error': _(
                'No se puede facturar: Aun no hay artículos a facturar para esta orden')}

        # ----------------------------------------------------------

        return {
            'order_id': order.id,
            'template': template,
        }

    @http.route('/q_l10n_mx_autoinvoice/select_address', type='json', auth='public', website=True, csrf=False)
    def autoinvoice_select_address(self, order_id, partner_id):
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
    # ----------------------------------------------------------
    @http.route('/q_l10n_mx_autoinvoice/add_address', type='json', auth='public', website=True, csrf=False)
    def autoinvoice_add_address(
        self, name=False, vat=False, email=False, phone=False,
        street_name=False, street_number=False,
        l10n_mx_edi_colony=False, l10n_mx_edi_locality=False,
        city=False, zipcode=False, country_id=False, state_id=False
    ):
        vals = normalize_values({
            'name': name,
            'vat': vat,
            'email': email,
            'phone': phone,
            'street_name': street_name,
            'street_number': street_number,
            'l10n_mx_edi_colony': l10n_mx_edi_colony,
            'l10n_mx_edi_locality': l10n_mx_edi_locality,
            'city': city,
            'zipcode': zipcode,
        })

        return super().autoinvoice_add_address(
            vals['name'], vals['vat'], vals['email'], vals['phone'],
            vals['street_name'], vals['street_number'],
            vals['l10n_mx_edi_colony'], vals['l10n_mx_edi_locality'],
            vals['city'], vals['zipcode'],
            country_id, state_id
        )

    # ----------------------------------------------------------
    #Normalización en INFORMATION
    # ----------------------------------------------------------
    @http.route('/q_l10n_mx_autoinvoice/information', type='json', auth='public', website=True, csrf=False)
    def autoinvoice_information(self, invoice_id, fiscal_regime=False, use_of_cfdi=False, payment_method=False):
        fiscal_regime = normalize_text(fiscal_regime)
        use_of_cfdi = normalize_text(use_of_cfdi)
        payment_method = normalize_text(payment_method)

        return super().autoinvoice_information(
            invoice_id, fiscal_regime, use_of_cfdi, payment_method
        )

    # ----------------------------------------------------------
    #Normalización en VALIDATE_INVOICE
    # ----------------------------------------------------------
    @http.route('/q_l10n_mx_autoinvoice/validate_invoice', type='json', auth='public', website=True, csrf=False)
    def autoinvoice_validate_invoice(self, invoice_id, fiscal_regime=False, use_of_cfdi=False, payment_method=False):
        fiscal_regime = normalize_text(fiscal_regime)
        use_of_cfdi = normalize_text(use_of_cfdi)
        payment_method = normalize_text(payment_method)

        return super().autoinvoice_validate_invoice(
            invoice_id, fiscal_regime, use_of_cfdi, payment_method
        )
