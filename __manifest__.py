{
    'name': 'Picking Priority Agent',
    'version': '1.1',
    'category': 'Warehouse',
    'summary': 'Explainable rule-based prioritization for warehouse pickings',
    'description': 'Adds configurable priority scoring, ranking, explanations, overrides, and audit logging for stock pickings.',
    'depends': ['stock', 'sale_stock'],
    'data': [
        'security/ir.model.access.csv',
        'views/res_partner_views.xml',
        'views/stock_picking_views.xml',
        'views/stock_picking_priority_dashboard.xml',
        'views/ai_copilot_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'odoo_picking_priority/static/src/js/complexity_factor_tooltip.js',
            'odoo_picking_priority/static/src/js/priority_queue_action.js',
        ],
    },
    'installable': True,
    'application': False,
}
