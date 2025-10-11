{
    'name': 'Refacturador',
    'version': '2.1.1',
    'summary': 'Extiende q_l10n_mx_autoinvoice para re-facturación a clientes',
    'depends': [
        'q_l10n_mx_autoinvoice',
        'website',
        'sale',
        'account',
        'l10n_mx_edi',
    ],
    'data': [
        'views/website_templates.xml',
        'views/assets.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'q_l10n_mx_autoinvoice_extend/static/src/js/autoinvoice_extend.js',
        ],
    },
    'author': 'Sergio Gil',
    'category': 'Accounting',
    'installable': True,
    'application': False,
    'auto_install': False,
}
