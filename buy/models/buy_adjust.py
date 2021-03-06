# -*- coding: utf-8 -*-

from odoo import fields, models, api
import odoo.addons.decimal_precision as dp
from odoo.exceptions import UserError

# 订单审核状态可选值
BUY_ORDER_STATES = [
    ('draft', u'未审核'),
    ('done', u'已审核'),
]

# 字段只读状态
READONLY_STATES = {
    'done': [('readonly', True)],
}


class buy_adjust(models.Model):
    _name = "buy.adjust"
    _inherit = ['mail.thread']
    _description = u"采购调整单"
    _order = 'date desc, id desc'

    name = fields.Char(u'单据编号', copy=False,
                       help=u'调整单编号，保存时可自动生成')
    order_id = fields.Many2one('buy.order', u'原始单据', states=READONLY_STATES,
                             copy=False, ondelete='restrict',
                             help=u'要调整的原始购货订单')
    date = fields.Date(u'单据日期', states=READONLY_STATES,
                       default=lambda self: fields.Date.context_today(self),
                       select=True, copy=False,
                       help=u'调整单创建日期，默认是当前日期')
    line_ids = fields.One2many('buy.adjust.line', 'order_id', u'调整单行',
                               states=READONLY_STATES, copy=True,
                               help=u'调整单明细行，不允许为空')
    approve_uid = fields.Many2one('res.users', u'审核人',
                            copy=False, ondelete='restrict',
                            help=u'审核调整单的人')
    state = fields.Selection(BUY_ORDER_STATES, u'审核状态',
                             select=True, copy=False,
                             default='draft',
                             help=u'调整单审核状态')
    note = fields.Text(u'备注',
                       help=u'单据备注')

    @api.multi
    def unlink(self):
        for order in self:
            if order.state == 'done':
                raise UserError(u'不能删除已审核的单据')

        return super(buy_adjust, self).unlink()

    def _get_vals(self, line):
        '''返回创建 buy order line 时所需数据'''
        return {
            'order_id': self.order_id.id,
            'goods_id': line.goods_id.id,
            'attribute_id': line.attribute_id.id,
            'quantity': line.quantity,
            'uom_id': line.uom_id.id,
            'price_taxed': line.price_taxed,
            'discount_rate': line.discount_rate,
            'discount_amount': line.discount_amount,
            'tax_rate': line.tax_rate,
            'note': line.note or '',
        }

    @api.one
    def buy_adjust_done(self):
        '''审核采购调整单：
        当调整后数量 < 原单据中已入库数量，则报错；
        当调整后数量 > 原单据中已入库数量，则更新原单据及入库单分单的数量；
        当调整后数量 = 原单据中已入库数量，则更新原单据数量，删除入库单分单；
        当新增产品时，则更新原单据及入库单分单明细行。
        '''
        if self.state == 'done':
            raise UserError(u'请不要重复审核！')
        if not self.line_ids:
            raise UserError(u'请输入产品明细行！')
        for line in self.line_ids:
            if  line.price_taxed < 0:
                raise UserError(u'产品含税单价不能小于0！')
        buy_receipt = self.env['buy.receipt'].search(
                    [('order_id', '=', self.order_id.id),
                     ('state', '=', 'draft')])
        if not buy_receipt:
            raise UserError(u'采购入库单已全部入库，不能调整')
        for line in self.line_ids:
            origin_line = self.env['buy.order.line'].search(
                        [('goods_id', '=', line.goods_id.id),
                         ('attribute_id', '=', line.attribute_id.id),
                         ('order_id', '=', self.order_id.id)])
            if len(origin_line) > 1:
                raise UserError(u'要调整的商品%s在原始单据中不唯一' % line.goods_id.name)
            if origin_line:
                origin_line.quantity += line.quantity # 调整后数量
                origin_line.note = line.note
                if origin_line.quantity < origin_line.quantity_in:
                    raise UserError(u'%s调整后数量不能小于原订单已入库数量' % line.goods_id.name)
                elif origin_line.quantity > origin_line.quantity_in:
                    # 查找出原购货订单产生的草稿状态的入库单明细行，并更新它
                    move_line = self.env['wh.move.line'].search(
                                    [('buy_line_id', '=', origin_line.id),
                                     ('state', '=', 'draft')])
                    if move_line:
                        move_line.goods_qty += line.quantity
                        move_line.goods_uos_qty = (move_line.goods_id.conversion
                                                   and move_line.goods_qty / move_line.goods_id.conversion
                                                   or move_line.goods_qty)
                        move_line.note = line.note
                    else:
                        raise UserError(u'商品%s已全部入库，建议新建购货订单' % line.goods_id.name)
                # 调整后数量与已入库数量相等时，删除产生的入库单分单
                else:
                    buy_receipt.unlink()
            else:
                new_line = self.env['buy.order.line'].create(self._get_vals(line))
                receipt_line = []
                if line.goods_id.force_batch_one:
                    i = 0
                    while i < line.quantity:
                        i += 1
                        receipt_line.append(
                                    self.order_id.get_receipt_line(new_line, single=True))
                else:
                    receipt_line.append(self.order_id.get_receipt_line(new_line, single=False))
                buy_receipt.write({'line_in_ids': [(0, 0, li[0]) for li in receipt_line]})
        self.state = 'done'
        self.approve_uid = self._uid


class buy_adjust_line(models.Model):
    _name = 'buy.adjust.line'
    _description = u'采购调整单明细'

    @api.one
    @api.depends('goods_id')
    def _compute_using_attribute(self):
        '''返回订单行中产品是否使用属性'''
        self.using_attribute = self.goods_id.attribute_ids and True or False

    @api.one
    @api.depends('quantity', 'price_taxed', 'discount_amount', 'tax_rate')
    def _compute_all_amount(self):
        '''当订单行的数量、单价、折扣额、税率改变时，改变购货金额、税额、价税合计'''
        self.price = (self.tax_rate != -100
                      and self.price_taxed / (1 + self.tax_rate * 0.01) or 0)
        self.amount = self.quantity * self.price - self.discount_amount  # 折扣后金额
        self.tax_amount = self.amount * self.tax_rate * 0.01  # 税额
        self.subtotal = self.amount + self.tax_amount

    order_id = fields.Many2one('buy.adjust', u'订单编号', select=True,
                               required=True, ondelete='cascade',
                               help=u'关联的调整单编号')
    goods_id = fields.Many2one('goods', u'商品', ondelete='restrict',
                               help=u'商品')
    using_attribute = fields.Boolean(u'使用属性', compute=_compute_using_attribute,
                                     help=u'商品是否使用属性')
    attribute_id = fields.Many2one('attribute', u'属性',
                                   ondelete='restrict',
                                   domain="[('goods_id', '=', goods_id)]",
                                   help=u'商品的属性，当商品有属性时，该字段必输')
    uom_id = fields.Many2one('uom', u'单位', ondelete='restrict',
                             help=u'商品计量单位')
    quantity = fields.Float(u'调整数量', default=1,
                            digits=dp.get_precision('Quantity'),
                            help=u'相对于原单据对应明细行的调整数量，可正可负')
    price = fields.Float(u'购货单价', compute=_compute_all_amount,
                         store=True, readonly=True,
                         digits=dp.get_precision('Amount'),
                         help=u'不含税单价，由含税单价计算得出')
    price_taxed = fields.Float(u'含税单价',
                               digits=dp.get_precision('Amount'),
                               help=u'含税单价，取自商品成本')
    discount_rate = fields.Float(u'折扣率%',
                                 help=u'折扣率')
    discount_amount = fields.Float(u'折扣额',
                                   digits=dp.get_precision('Amount'),
                                   help=u'输入折扣率后自动计算得出，也可手动输入折扣额')
    amount = fields.Float(u'金额', compute=_compute_all_amount,
                          store=True, readonly=True,
                          digits=dp.get_precision('Amount'),
                          help=u'金额  = 价税合计  - 税额')
    tax_rate = fields.Float(u'税率(%)', default=lambda self:self.env.user.company_id.import_tax_rate,
                            help=u'默认值取公司进项税率')
    tax_amount = fields.Float(u'税额', compute=_compute_all_amount,
                              store=True, readonly=True,
                              digits=dp.get_precision('Amount'),
                              help=u'由税率计算得出')
    subtotal = fields.Float(u'价税合计', compute=_compute_all_amount,
                            store=True, readonly=True,
                            digits=dp.get_precision('Amount'),
                            help=u'含税单价 乘以 数量')
    note = fields.Char(u'备注',
                       help=u'本行备注')

    @api.onchange('goods_id')
    def onchange_goods_id(self):
        '''当订单行的产品变化时，带出产品上的单位、默认仓库、成本价'''
        if self.goods_id:
            self.uom_id = self.goods_id.uom_id
            if not self.goods_id.cost:
                raise UserError(u'请先设置商品的成本！')
            self.price_taxed = self.goods_id.cost

    @api.onchange('quantity', 'price_taxed', 'discount_rate')
    def onchange_discount_rate(self):
        '''当数量、单价或优惠率发生变化时，优惠金额发生变化'''
        price = (self.tax_rate != -100
                 and self.price_taxed / (1 + self.tax_rate * 0.01) or 0)
        self.discount_amount = (self.quantity * price *
                                self.discount_rate * 0.01)
