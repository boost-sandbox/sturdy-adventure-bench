# Generated by Django 3.2.18 on 2023-07-06 09:43
from decimal import Decimal

import graphene
from dataclasses import dataclass
from typing import Dict, List

from babel.numbers import get_currency_precision
from django.db import migrations
from django.apps import apps as registry
from django.db.models import Exists, OuterRef
from django.db.models.signals import post_migrate

from .tasks.saleor3_17 import update_discounted_prices_task

# The batch of size 100 takes ~0.9 second and consumes ~30MB memory at peak
BATCH_SIZE = 100


def run_migration(apps, _schema_editor):
    Promotion = apps.get_model("discount", "Promotion")
    PromotionRule = apps.get_model("discount", "PromotionRule")
    SaleChannelListing = apps.get_model("discount", "SaleChannelListing")
    SaleTranslation = apps.get_model("discount", "SaleTranslation")
    Sale = apps.get_model("discount", "Sale")
    PromotionTranslation = apps.get_model("discount", "PromotionTranslation")
    CheckoutLineDiscount = apps.get_model("discount", "CheckoutLineDiscount")
    OrderLineDiscount = apps.get_model("discount", "OrderLineDiscount")
    OrderLine = apps.get_model("order", "OrderLine")

    @dataclass
    class RuleInfo:
        rule: PromotionRule
        sale_id: int
        channel_id: int

    sales_listing = SaleChannelListing.objects.order_by("sale_id")
    for sale_listing_batch_pks in channel_listing_in_batches(sales_listing):
        sales_listing_batch = (
            SaleChannelListing.objects.filter(pk__in=sale_listing_batch_pks)
            .order_by("sale_id")
            .prefetch_related(
                "sale",
                "sale__collections",
                "sale__categories",
                "sale__products",
                "sale__variants",
            )
        )
        sales_batch_pks = {listing.sale_id for listing in sales_listing_batch}

        saleid_promotion_map: Dict[int, Promotion] = {}
        rules_info: List[RuleInfo] = []

        migrate_sales_to_promotions(
            Sale, Promotion, sales_batch_pks, saleid_promotion_map
        )
        migrate_sale_listing_to_promotion_rules(
            RuleInfo,
            PromotionRule,
            sales_listing_batch,
            saleid_promotion_map,
            rules_info,
        )
        migrate_translations(
            SaleTranslation, PromotionTranslation, sales_batch_pks, saleid_promotion_map
        )

        rule_by_channel_and_sale = get_rule_by_channel_sale(rules_info)
        migrate_checkout_line_discounts(
            CheckoutLineDiscount, sales_batch_pks, rule_by_channel_and_sale
        )
        migrate_order_line_discounts(
            OrderLine, OrderLineDiscount, sales_batch_pks, rule_by_channel_and_sale
        )

    # migrate sales not listed in any channel
    sales_not_listed = Sale.objects.filter(
        ~Exists(sales_listing.filter(sale_id=OuterRef("pk")))
    ).order_by("pk")
    for sales_batch_pks in queryset_in_batches(sales_not_listed):
        saleid_promotion_map = {}
        migrate_sales_to_promotions(
            Sale, Promotion, sales_batch_pks, saleid_promotion_map
        )
        migrate_sales_to_promotion_rules(
            Sale, PromotionRule, sales_batch_pks, saleid_promotion_map
        )
        migrate_translations(
            SaleTranslation, PromotionTranslation, sales_batch_pks, saleid_promotion_map
        )


def migrate_sales_to_promotions(Sale, Promotion, sales_pks, saleid_promotion_map):
    if sales := Sale.objects.filter(pk__in=sales_pks).order_by("pk"):
        for sale in sales:
            saleid_promotion_map[sale.id] = convert_sale_into_promotion(Promotion, sale)
        Promotion.objects.bulk_create(saleid_promotion_map.values())


def convert_sale_into_promotion(Promotion, sale):
    return Promotion(
        name=sale.name,
        old_sale_id=sale.id,
        start_date=sale.start_date,
        end_date=sale.end_date,
        created_at=sale.created_at,
        updated_at=sale.updated_at,
        metadata=sale.metadata,
        private_metadata=sale.private_metadata,
        last_notification_scheduled_at=sale.notification_sent_datetime,
    )


def create_promotion_rule(
    PromotionRule, sale, promotion, discount_value=None, old_channel_listing_id=None
):
    return PromotionRule(
        promotion=promotion,
        catalogue_predicate=create_catalogue_predicate_from_sale(sale),
        reward_value_type=sale.type,
        reward_value=discount_value,
        old_channel_listing_id=old_channel_listing_id,
    )


def migrate_sale_listing_to_promotion_rules(
    RuleInfo,
    PromotionRule,
    sale_listings,
    saleid_promotion_map,
    rules_info,
):
    if sale_listings:
        for sale_listing in sale_listings:
            promotion = saleid_promotion_map[sale_listing.sale_id]
            rules_info.append(
                RuleInfo(
                    rule=create_promotion_rule(
                        PromotionRule,
                        sale_listing.sale,
                        promotion,
                        sale_listing.discount_value,
                        sale_listing.id,
                    ),
                    sale_id=sale_listing.sale_id,
                    channel_id=sale_listing.channel_id,
                )
            )

        promotion_rules = [rules_info.rule for rules_info in rules_info]
        PromotionRule.objects.bulk_create(promotion_rules)

        PromotionRuleChannel = PromotionRule.channels.through
        rules_channels = [
            PromotionRuleChannel(
                promotionrule_id=rule_info.rule.id, channel_id=rule_info.channel_id
            )
            for rule_info in rules_info
        ]
        PromotionRuleChannel.objects.bulk_create(rules_channels)


def create_catalogue_predicate_from_sale(sale):
    collection_ids = [
        graphene.Node.to_global_id("Collection", pk)
        for pk in sale.collections.values_list("pk", flat=True)
    ]
    category_ids = [
        graphene.Node.to_global_id("Category", pk)
        for pk in sale.categories.values_list("pk", flat=True)
    ]
    product_ids = [
        graphene.Node.to_global_id("Product", pk)
        for pk in sale.products.values_list("pk", flat=True)
    ]
    variant_ids = [
        graphene.Node.to_global_id("ProductVariant", pk)
        for pk in sale.variants.values_list("pk", flat=True)
    ]
    return create_catalogue_predicate(
        collection_ids, category_ids, product_ids, variant_ids
    )


def create_catalogue_predicate(collection_ids, category_ids, product_ids, variant_ids):
    predicate: Dict[str, List] = {"OR": []}
    if collection_ids:
        predicate["OR"].append({"collectionPredicate": {"ids": collection_ids}})
    if category_ids:
        predicate["OR"].append({"categoryPredicate": {"ids": category_ids}})
    if product_ids:
        predicate["OR"].append({"productPredicate": {"ids": product_ids}})
    if variant_ids:
        predicate["OR"].append({"variantPredicate": {"ids": variant_ids}})
    if not predicate.get("OR"):
        predicate = {}

    return predicate


def migrate_sales_to_promotion_rules(
    Sale, PromotionRule, sales_pks, saleid_promotion_map
):
    if sales := Sale.objects.filter(pk__in=sales_pks).order_by("pk"):
        rules: List[PromotionRule] = []
        for sale in sales:
            promotion = saleid_promotion_map[sale.id]
            rules.append(create_promotion_rule(PromotionRule, sale, promotion))
        PromotionRule.objects.bulk_create(rules)


def migrate_translations(
    SaleTranslation, PromotionTranslation, sales_pks, saleid_promotion_map
):
    if sale_translations := SaleTranslation.objects.filter(sale_id__in=sales_pks):
        promotion_translations = [
            PromotionTranslation(
                name=translation.name,
                language_code=translation.language_code,
                promotion=saleid_promotion_map[translation.sale_id],
            )
            for translation in sale_translations
        ]
        PromotionTranslation.objects.bulk_create(promotion_translations)


def migrate_checkout_line_discounts(
    CheckoutLineDiscount, sales_pks, rule_by_channel_and_sale
):
    if checkout_line_discounts := CheckoutLineDiscount.objects.filter(
        sale_id__in=sales_pks
    ).select_related("line__checkout"):
        for checkout_line_discount in checkout_line_discounts:
            if checkout_line := checkout_line_discount.line:
                channel_id = checkout_line.checkout.channel_id
                sale_id = checkout_line_discount.sale_id
                lookup = f"{channel_id}_{sale_id}"
                checkout_line_discount.type = "promotion"
                if promotion_rule := rule_by_channel_and_sale.get(lookup):
                    checkout_line_discount.promotion_rule = promotion_rule

        CheckoutLineDiscount.objects.bulk_update(
            checkout_line_discounts, ["promotion_rule_id", "type"]
        )


def migrate_order_line_discounts(
    OrderLine, OrderLineDiscount, sales_pks, rule_by_channel_and_sale
):
    global_pks = [graphene.Node.to_global_id("Sale", pk) for pk in sales_pks]
    if order_lines := OrderLine.objects.filter(sale_id__in=global_pks).prefetch_related(
        "order"
    ):
        order_line_discounts = []
        for order_line in order_lines:
            channel_id = order_line.order.channel_id
            sale_id = graphene.Node.from_global_id(order_line.sale_id)[1]
            lookup = f"{channel_id}_{sale_id}"
            if rule := rule_by_channel_and_sale.get(lookup):
                order_line_discounts.append(
                    OrderLineDiscount(
                        type="promotion",
                        value_type=rule.reward_value_type,
                        value=rule.reward_value,
                        amount_value=get_discount_amount_value(order_line),
                        currency=order_line.currency,
                        promotion_rule=rule,
                        line=order_line,
                    )
                )

        OrderLineDiscount.objects.bulk_create(order_line_discounts)


def get_discount_amount_value(order_line):
    precision = get_currency_precision(order_line.currency)
    number_places = Decimal(10) ** -precision
    price = order_line.quantity * order_line.unit_discount_amount
    return price.quantize(number_places)


def get_rule_by_channel_sale(rules_info):
    return {
        f"{rule_info.channel_id}_{rule_info.sale_id}": rule_info.rule
        for rule_info in rules_info
    }


def channel_listing_in_batches(qs):
    first_sale_id = 0
    while True:
        batch_1 = qs.values("sale_id").filter(sale_id__gt=first_sale_id)[:BATCH_SIZE]
        if len(batch_1) == 0:
            break
        last_sale_id = batch_1[len(batch_1) - 1]["sale_id"]

        # `batch_2` extends initial `batch_1` to include all records from
        # `SaleChannelListing` which refer to `last_sale_id`
        batch_2 = qs.values("pk", "sale_id").filter(
            sale_id__gt=first_sale_id, sale_id__lte=last_sale_id
        )
        pks = [v["pk"] for v in batch_2]
        if not pks:
            break
        yield pks
        first_sale_id = batch_2[len(batch_2) - 1]["sale_id"]


def queryset_in_batches(queryset):
    start_pk = 0
    while True:
        qs = queryset.values("pk").filter(pk__gt=start_pk)[:BATCH_SIZE]
        pks = [v["pk"] for v in qs]
        if not pks:
            break
        yield pks
        start_pk = pks[-1]


def update_discounted_prices(apps, _schema_editor):
    def on_migrations_complete(sender=None, **kwargs):
        update_discounted_prices_task.delay()

    sender = registry.get_app_config("discount")
    post_migrate.connect(on_migrations_complete, weak=False, sender=sender)


class Migration(migrations.Migration):
    dependencies = [
        ("discount", "0046_promotion_discount_indexes"),
    ]

    operations = [
        migrations.RunPython(run_migration, migrations.RunPython.noop),
        migrations.RunPython(
            update_discounted_prices, reverse_code=migrations.RunPython.noop
        ),
    ]
