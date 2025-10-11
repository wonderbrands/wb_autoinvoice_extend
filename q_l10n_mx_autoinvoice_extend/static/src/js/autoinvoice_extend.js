odoo.define('q_l10n_mx_autoinvoice_extend.autoinvoice_override', function (require) {
'use strict';

var publicWidget = require('web.public.widget');
var core = require('web.core');

// Obtenemos el widget original del registro para poder extenderlo
var AutoinvoicePage = publicWidget.registry.q_l10n_mx_autoinvoice;

// Si el widget original no existe, detenemos para evitar errores
if (!AutoinvoicePage) {
    return;
}

// Usamos .include() para extender la funcionalidad del widget original
AutoinvoicePage.include({

    /**
     * @override
     * Sobrescribimos la función de validación del formulario de dirección.
     */
    validateForm: function ($form) {
        const errors = {};
        const values = {};
        const requiredFields = ['name', 'vat', 'zipcode'];

        $form.serializeArray().forEach(function (item) {
            values[item.name] = item.value;
        });

        requiredFields.forEach(function (fieldName) {
            if (!values[fieldName] || values[fieldName].trim() === '') {
                errors[fieldName] = `El campo es requerido`;
            }
        });

        // Validación específica para RFC
        if (values.vat && (values.vat.length < 12 || values.vat.length > 13)) {
            errors.vat = 'El RFC debe tener 12 o 13 caracteres.';
        }

        // Validación específica para Código Postal
        if (values.zipcode && values.zipcode.length !== 5) {
            errors.zipcode = 'El Código Postal debe tener 5 dígitos.';
        }

        return {
            values: values,
            errors: errors,
        };
    },

    /**
     * @override
     * Sobrescribimos la función que se ejecuta al hacer clic en el botón de la dirección.
     */
    selectAddress: async function (ev) {
        this.disableButton(ev);
        const $form = this.$el.find('form');
        const { values, errors } = this.validateForm($form); // Llama a nuestra nueva función de validación

        // Limpiar errores previos
        $form.find('.is-invalid').removeClass('is-invalid');
        $form.find('.invalid-feedback').empty();

        if (Object.keys(errors).length) {
            // Mostrar los nuevos errores
            Object.keys(errors).map(name => {
                const $div = $form.find(`.div_${name.replace('_id', '')}`);
                $div.find(`[name="${name}"]`).addClass('is-invalid')
                $div.find('.invalid-feedback').html(errors[name]);
            });
            this.enableButton(ev);
        } else {
            // Si la validación es exitosa, preparamos SOLO los datos necesarios
            const params_to_send = {
                name: values.name,
                vat: values.vat,
                zipcode: values.zipcode,
                email: values.email || '', // El email es útil pero opcional
            };

            const res = await this._addAddress(params_to_send); // Llama al backend

            if (res.error) {
                this.showError(res.error);
                this.enableButton(ev);
            } else {
                this.partner_id = res.partner_id;
                const resp = await this._selectAddress();
                if (resp.error) {
                    this.showError(resp.error);
                    this.enableButton(ev);
                } else {
                    this.invoice_id = resp.invoice_id;
                    $form.empty().append(resp.template);
                }
            }
        }
    },
});

});