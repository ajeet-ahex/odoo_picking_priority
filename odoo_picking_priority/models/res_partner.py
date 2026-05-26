from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    x_customer_sla_days = fields.Float(
        string="Customer SLA (Days)",
        help="Customer-specific SLA expressed in days, similar to product lead time.",
    )
