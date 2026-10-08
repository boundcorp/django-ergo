from django.db import models
from django_ergo.bots import BotTable


class AdDailyStat(BotTable):
    """Meta ad delivery per campaign per day (account timezone)"""

    date = models.DateField()
    account_id = models.CharField(max_length=32)
    campaign_id = models.CharField(max_length=32)
    campaign_name = models.CharField(max_length=300, blank=True)
    spend = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    impressions = models.IntegerField(null=True)
    reach = models.IntegerField(null=True)
    clicks = models.IntegerField(null=True)
    link_clicks = models.IntegerField(null=True)
    outbound_clicks = models.IntegerField(null=True)
    landing_page_views = models.IntegerField(null=True)
    installs = models.IntegerField(null=True)
    purchases = models.IntegerField(null=True)
    cpc = models.DecimalField(max_digits=14, decimal_places=4, null=True)
    cpc_all = models.DecimalField(max_digits=14, decimal_places=4, null=True)
    cpi = models.DecimalField(max_digits=14, decimal_places=4, null=True)
    actions = models.JSONField(default=list)
    pulled_at = models.DateTimeField(null=True)
    partial = models.BooleanField(default=False)

    class Meta:
        unique_together = [("date", "campaign_id")]
        ordering = ["-date", "campaign_name"]


class AdCampaignSummary(BotTable):
    """One row per campaign over the latest pull window, with CPC and CPI computed by Ad Manager.

    cpc is spend per link click, cpc_all is spend per click of any kind, cpi is spend per Meta-reported install.
    """

    window_start = models.DateField()
    window_end = models.DateField()
    account_id = models.CharField(max_length=32)
    campaign_id = models.CharField(max_length=32, unique=True)
    campaign_name = models.CharField(max_length=300, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    spend = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    impressions = models.IntegerField(null=True)
    clicks = models.IntegerField(null=True)
    link_clicks = models.IntegerField(null=True)
    landing_page_views = models.IntegerField(null=True)
    installs = models.IntegerField(null=True)
    cpc = models.DecimalField(max_digits=14, decimal_places=4, null=True)
    cpc_all = models.DecimalField(max_digits=14, decimal_places=4, null=True)
    cpi = models.DecimalField(max_digits=14, decimal_places=4, null=True)
    pulled_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ["campaign_name"]


class AdTotal(BotTable):
    """All campaigns combined over the latest pull window, one row per currency, with CPC and CPI from Ad Manager."""

    window_start = models.DateField()
    window_end = models.DateField()
    currency = models.CharField(max_length=3, unique=True)
    spend = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    clicks = models.IntegerField(null=True)
    link_clicks = models.IntegerField(null=True)
    installs = models.IntegerField(null=True)
    cpc = models.DecimalField(max_digits=14, decimal_places=4, null=True)
    cpc_all = models.DecimalField(max_digits=14, decimal_places=4, null=True)
    cpi = models.DecimalField(max_digits=14, decimal_places=4, null=True)
    pulled_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ["currency"]
