from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.fields import Datetime


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # Esta función SOLO se encarga de crear la NC en estado borrador.
    # Es llamada al inicio del flujo para desbloquear la orden de venta.
    def _create_draft_credit_note_for_autoinvoice(self, global_invoice):
        """
        Crea una Nota de Crédito en estado BORRADOR.
        NO la publica, timbra ni reconcilia. Simplemente la crea y la devuelve.
        """
        self.ensure_one()
        #print(f"Creando NC en BORRADOR para la orden {self.name}")

        # Lógica de validación
        existing_refund = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_refund' and m.state in ('draft', 'posted')
        )
        if existing_refund:
            raise UserError(_("Ya existe una nota de crédito asociada a esta orden."))
        if not global_invoice or not (
                global_invoice.move_type == 'out_invoice' and global_invoice.state == 'posted' and global_invoice.partner_id.vat == 'XAXX010101000'):
            raise UserError(_("No se proporcionó una factura global válida y publicada para crear la NC."))

        #print(f"Generando borrador de NC desde la factura global {global_invoice.name}")

        # --- Lógica para preparar los valores de la NC ---
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
        invoice_lines = []
        for line in self.order_line:
            # Lógica para crear líneas de factura
            income_account = (
                    line.product_id.property_account_income_id or
                    line.product_id.categ_id.property_account_income_categ_id
            )
            if not income_account:
                raise UserError(
                    _("No se pudo encontrar cuenta contable para el producto '%s'.") % line.product_id.display_name)
            line_vals = (0, 0, {
                'product_id': line.product_id.id, 'name': line.name, 'quantity': line.product_uom_qty,
                'price_unit': line.price_unit, 'account_id': income_account.id, 'tax_ids': [(6, 0, line.tax_id.ids)],
                'product_uom_id': line.product_uom.id, 'sale_line_ids': [(6, 0, [line.id])],
                'analytic_account_id': line.order_id.analytic_account_id.id if line.order_id.analytic_account_id else False,
            })
            invoice_lines.append(line_vals)

        refund_vals['invoice_line_ids'] = invoice_lines

        # --- Creación y devolución del borrador ---
        refund_draft = self.env['account.move'].create(refund_vals)
        refund_draft._recompute_dynamic_lines(recompute_all_taxes=True)
        return refund_draft

    # Esta función toma un borrador de NC y lo procesa por completo.
    # Se llama al FINAL del flujo, solo si la factura del cliente tuvo éxito.
    def _commit_credit_note_for_autoinvoice(self, credit_note_draft, global_invoice):
        """
        Publica, timbra y reconcilia una Nota de Crédito en borrador.
        """
        #print(f"Haciendo 'commit' de la NC {credit_note_draft.name}: publicando, timbrando y reconciliando.")

        credit_note_draft.action_post()
        try:
            # button_process_edi_web_services busca documentos EDI pendientes y los envía al PAC
            credit_note_draft.button_process_edi_web_services()
        except Exception as e:
            pass

        try:
            inv_receivable_lines = global_invoice.line_ids.filtered(
                lambda l: l.account_id.internal_type == 'receivable' and not l.full_reconcile_id)
            ref_receivable_lines = credit_note_draft.line_ids.filtered(
                lambda l: l.account_id.internal_type == 'receivable' and not l.full_reconcile_id)
            if inv_receivable_lines and ref_receivable_lines:
                (inv_receivable_lines | ref_receivable_lines).reconcile()
                #print(f"NC {credit_note_draft.name} reconciliada con Factura Global {global_invoice.name}.")
        except Exception as e:
            pass
            # print(f"Error al reconciliar NC {credit_note_draft.name}: {e}")

        credit_note_draft.message_post(
            body=f"<p>Nota de crédito generada y procesada automáticamente por refacturación de la orden <b>{self.name}</b>.</p>",
            message_type='notification',
            subtype_id=self.env.ref('mail.mt_note').id,
        )
        return True