import time

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.fields import Datetime

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # CON UNA NOTA DE CREDITO DESDE CERO
    def _reprocess_from_global_invoice(self, global_invoice):
        self.ensure_one()
        #print(f"Iniciando reprocesamiento de orden {self.name}")

        # Validar si ya existe NC
        existing_refund = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_refund' and m.state in ('draft', 'posted')
        )
        if existing_refund:
            raise UserError(_("Ya existe una nota de crédito asociada a esta orden."))

        # Validar que sea factura global
        global_invoice = global_invoice.filtered(lambda m: (
                m.move_type == 'out_invoice' and
                m.state == 'posted' and
                m.partner_id.vat == 'XAXX010101000'
        ))
        if not global_invoice:
            raise UserError(_("No hay una factura global válida y publicada."))

        global_invoice = global_invoice[0] # La factura siempre es la primera
        #print(f"Generando nueva nota de crédito (manual) basada en la orden: {self.name}")

        # Crear encabezado de NC
        refund_vals = {
            'move_type': 'out_refund',
            'invoice_origin': f"Nota de crédito por refacturación de {self.name}",
            'ref': f"NC para: {self.name}",
            'partner_id': global_invoice.partner_id.id,
            'journal_id': global_invoice.journal_id.id,
            'invoice_date': fields.Date.today(),
            'date': fields.Date.today(),
            'l10n_mx_edi_usage': global_invoice.l10n_mx_edi_usage,
            'l10n_mx_edi_origin': f"01|{global_invoice.l10n_mx_edi_cfdi_uuid}",
            'l10n_mx_edi_payment_method_id': global_invoice.l10n_mx_edi_payment_method_id.id,
            'invoice_line_ids': [],
            'from_autoinvoice': True,
            'team_id': self.team_id.id,
        }

        # Preparar líneas de productos
        invoice_lines = []
        for line in self.order_line:
            income_account = (
                    line.product_id.property_account_income_id or
                    line.product_id.categ_id.property_account_income_categ_id
            )
            if not income_account:
                raise UserError(_(
                    "No se pudo encontrar cuenta contable para el producto '%s'."
                ) % line.product_id.display_name)

            line_vals = (0, 0, {
                'product_id': line.product_id.id,
                'name': line.name,
                'quantity': line.product_uom_qty,
                'price_unit': line.price_unit,
                'account_id': income_account.id,
                'tax_ids': [(6, 0, line.tax_id.ids)],
                'product_uom_id': line.product_uom.id,
                'sale_line_ids': [(6, 0, [line.id])],
                'analytic_account_id': line.order_id.analytic_account_id.id if line.order_id.analytic_account_id else False,
            })
            invoice_lines.append(line_vals)


            #print(line_vals)

        refund_vals['invoice_line_ids'] = invoice_lines
        #print(invoice_lines)
        refund = self.env['account.move'].create(refund_vals)

        # Recalcular
        refund._recompute_dynamic_lines(recompute_all_taxes=True)

        # Confirmar
        refund.action_post()

        # Timbrar
        refund.button_process_edi_web_services()

        # Agregar mensaje
        refund.message_post(
            body=f"<p>Nota de crédito generada automáticamente por refacturación de la orden <b>{self.name}</b> a partir de la factura global <b>{global_invoice.name}</b>.</p>",
            message_type='notification',
            subtype_id=self.env.ref('mail.mt_note').id,
        )

        return refund

