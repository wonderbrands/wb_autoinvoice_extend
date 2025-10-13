odoo.define('q_l10n_mx_autoinvoice_extend.autoinvoice_override', function (require) {
'use strict';

    var publicWidget = require('web.public.widget');
    var core = require('web.core');
    // qweb ya no es necesario para mostrar errores, pero lo dejamos por si otras funciones lo usan.
    var qweb = core.qweb;

    // Obtenemos el widget original del registro para poder extenderlo
    var AutoinvoicePage = publicWidget.registry.q_l10n_mx_autoinvoice;

    // Si el widget original no se encuentra, detenemos para evitar errores.
    if (!AutoinvoicePage) {
        return;
    }

    // Usamos .include() para extender la funcionalidad del widget original
    AutoinvoicePage.include({

        /**
         * @override
         * Se ejecuta al cargar la página para mostrar errores guardados en sesión.
         */
        start: function () {
            var self = this;
            return this._super.apply(this, arguments).then(function () {
                var errorMessage = sessionStorage.getItem('autoinvoice_error');
                if (errorMessage) {
                    self.showError(errorMessage);
                    sessionStorage.removeItem('autoinvoice_error');
                }
            });
        },

        /**
         * @override
         * Mantenemos tu función de validación simplificada para el formulario de dirección.
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

            if (values.vat && (values.vat.length < 12 || values.vat.length > 13)) {
                errors.vat = 'El RFC debe tener 12 o 13 caracteres.';
            }

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
         * Mantenemos la función que maneja el envío del formulario de dirección.
         */
        selectAddress: async function (ev) {
            this.disableButton(ev);
            const $form = this.$el.find('form');
            const { values, errors } = this.validateForm($form);

            $form.find('.is-invalid').removeClass('is-invalid');
            $form.find('.invalid-feedback').empty();

            if (Object.keys(errors).length) {
                Object.keys(errors).map(name => {
                    const $div = $form.find(`.div_${name.replace('_id', '')}`);
                    $div.find(`[name="${name}"]`).addClass('is-invalid')
                    $div.find('.invalid-feedback').html(errors[name]);
                });
                this.enableButton(ev);
            } else {
                const params_to_send = {
                    name: values.name,
                    vat: values.vat,
                    zipcode: values.zipcode,
                    email: values.email || '',
                };
                const res = await this._addAddress(params_to_send);

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

        /**
         * @override
         * Mantenemos la función final que maneja la lógica de éxito, reinicio y error.
         */
        validateInvoice: async function(ev) {
            this.disableButton(ev);
            const $form = $(ev.currentTarget).closest('form');
            const { values } = this._getValues(this.$el.find('form'));

            const res = await this._validateInvoice(values);

            this.$el.parent().find('.autoinvoice_page_alert').empty();

            if (res.restart_flow) {
                sessionStorage.setItem('autoinvoice_error', res.error);
                window.location.href = '/autoinvoice';
            } else if (res.error) {
                this.showError(res.error);
                this.enableButton(ev);
            } else if (res.template) {
                this.invoice_success = true;
                this.enableButton(ev);
                $form.empty().append(res.template);
            } else {
                this.showError('Ocurrió una respuesta inesperada del servidor.');
                this.enableButton(ev);
            }
        },

        // =================================================================
        // CAMBIO FINAL A PRUEBA DE ERRORES
        // =================================================================
        /**
         * @override
         * Sobrescribimos la función showError para construir el HTML directamente aquí.
         * Esto evita por completo el error "Template not found" al no depender del motor QWeb.
         */
        showError: function(message) {
            const $errorContainer = this.$el.parent().find('.autoinvoice_page_alert');
            $errorContainer.empty();
            if (message) {
                // Sanitizamos el mensaje para prevenir ataques de seguridad (XSS)
                const escapedMessage = _.escape(message);

                // Construimos el HTML del mensaje de error como un string
                const errorHtml = `
                    <div class="alert alert-danger" role="alert" style="font-size: 1.1em; border-radius: 8px;">
                        <div class="d-flex align-items-center">
                            <i class="fa fa-exclamation-triangle fa-2x mr-3"></i>
                            <div>
                                <strong class="d-block">Error al Procesar la Factura:</strong>
                                <span>${escapedMessage}</span>
                            </div>
                        </div>
                    </div>
                `;
                // Agregamos el HTML al contenedor de alertas
                $errorContainer.append(errorHtml);
            }
        },
    });
});