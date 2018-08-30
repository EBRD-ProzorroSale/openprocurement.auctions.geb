# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, time

from schematics.exceptions import ValidationError
from schematics.transforms import blacklist, whitelist
from schematics.types import StringType, IntType, BooleanType
from schematics.types.compound import ModelType
from schematics.types.serializable import serializable
from pyramid.security import Allow
from zope.interface import implementer

from openprocurement.auctions.core.includeme import IAwardingNextCheck
from openprocurement.auctions.core.models import (
    Model,
    Administrator_role,
    AuctionParameters as BaseAuctionParameters,
    Auction as BaseAuction,
    BankAccount,
    BaseOrganization,
    Bid as BaseBid,
    Guarantee,
    IAuction,
    IsoDateTimeType,
    IsoDurationType,
    ListType,
    Lot,
    Period,
    Value,
    calc_auction_end_time,
    dgfCDB2Complaint,
    dgfCDB2Document,
    dgfCDB2Item,
    dgfCancellation,
    edit_role,
    get_auction,
    validate_items_uniq,
    validate_lots_uniq,
    validate_not_available,
)
from openprocurement.auctions.core.plugins.awarding.v2_1.models import Award
from openprocurement.auctions.core.plugins.contracting.v2_1.models import Contract
from openprocurement.auctions.core.utils import (
    SANDBOX_MODE,
    TZ,
    calculate_business_date,
    get_request_from_root,
    get_now,
    AUCTIONS_COMPLAINT_STAND_STILL_TIME as COMPLAINT_STAND_STILL_TIME, get_auction_creation_date
)

from openprocurement.auctions.landlease.constants import AUCTION_STATUSES
from .constants import (
    MINIMAL_EXPOSITION_PERIOD,
    MINIMAL_EXPOSITION_REQUIRED_FROM,
    MINIMAL_PERIOD_FROM_RECTIFICATION_END
)
from .utils import generate_rectificationPeriod                                 # TODO
from openprocurement.auctions.landlease.roles import (
    auction_create_role,
    auction_contractTerms_create_role
)


class LeaseTerms(Model):
    leaseDuration = IsoDurationType(required=True)


class ContractTerms(Model):

    class Options:
        roles = {
            'create': auction_contractTerms_create_role
        }
    type = StringType(choices=['lease'])
    leaseTerms = ModelType(LeaseTerms, required=True)


class AuctionParameters(BaseAuctionParameters):
    type = StringType(choices=['texas'])


def bids_validation_wrapper(validation_func):
    def validator(klass, data, value):
        orig_data = data
        while not isinstance(data['__parent__'], BaseAuction):
            # in case this validation wrapper is used for subelement of bid (such as parameters)
            # traverse back to the bid to get possibility to check status  # troo-to-to =)
            data = data['__parent__']
        if data['status'] in ('invalid', 'draft'):
            # skip not valid bids
            return
        tender = data['__parent__']
        request = tender.__parent__.request
        if request.method == "PATCH" and isinstance(tender, BaseAuction) and request.authenticated_role == "auction_owner":
            # disable bids validation on tender PATCH requests as tender bids will be invalidated
            return
        return validation_func(klass, orig_data, value)
    return validator


class Bid(BaseBid):
    class Options:
        roles = {
            'create': whitelist('value', 'tenderers', 'parameters', 'lotValues', 'status', 'qualified'),
        }

    status = StringType(choices=['active', 'draft', 'invalid'], default='active')
    documents = ListType(ModelType(dgfCDB2Document), default=list())
    qualified = BooleanType(required=True, choices=[True])

    @bids_validation_wrapper
    def validate_value(self, data, value):
        BaseBid._validator_functions['value'](self, data, value)


class Cancellation(dgfCancellation):
    documents = ListType(ModelType(dgfCDB2Document), default=list())


def rounding_shouldStartAfter(start_after, auction, use_from=datetime(2016, 6, 1, tzinfo=TZ)):
    if (auction.enquiryPeriod and auction.enquiryPeriod.startDate or get_now()) > use_from and not (SANDBOX_MODE and auction.submissionMethodDetails and u'quick' in auction.submissionMethodDetails):
        midnigth = datetime.combine(start_after.date(), time(0, tzinfo=start_after.tzinfo))
        if start_after >= midnigth:
            start_after = midnigth + timedelta(1)
    return start_after


class AuctionAuctionPeriod(Period):
    """The auction period."""

    @serializable(serialize_when_none=False)
    def shouldStartAfter(self):
        if self.endDate:
            return
        auction = self.__parent__
        if auction.lots or auction.status not in ['active.tendering', 'active.auction']:
            return
        if self.startDate and get_now() > calc_auction_end_time(auction.numberOfBids, self.startDate):
            start_after = calc_auction_end_time(auction.numberOfBids, self.startDate)
        elif auction.tenderPeriod and auction.tenderPeriod.endDate:
            start_after = auction.tenderPeriod.endDate
        else:
            return
        return rounding_shouldStartAfter(start_after, auction).isoformat()

    def validate_startDate(self, data, startDate):
        auction = get_auction(data['__parent__'])
        if not auction.revisions and not startDate:
            raise ValidationError(u'This field is required.')


class RectificationPeriod(Period):
    invalidationDate = IsoDateTimeType()


edit_role = (edit_role + blacklist('enquiryPeriod',
                                   'tenderPeriod',
                                   'auction_value',
                                   'auction_minimalStep',
                                   'auction_guarantee',
                                   'eligibilityCriteria',
                                   'eligibilityCriteria_en',
                                   'eligibilityCriteria_ru',
                                   'awardCriteriaDetails',
                                   'awardCriteriaDetails_en',
                                   'awardCriteriaDetails_ru',
                                   'procurementMethodRationale',
                                   'procurementMethodRationale_en',
                                   'procurementMethodRationale_ru',
                                   'submissionMethodDetails',
                                   'submissionMethodDetails_en',
                                   'submissionMethodDetails_ru',
                                   'minNumberOfQualifiedBids'))

Administrator_role = (Administrator_role + whitelist('awards'))


class ILandLeaseAuction(IAuction):
    """Marker interface for LandLease auctions"""


@implementer(ILandLeaseAuction)
class Auction(BaseAuction):

    class Options:
        roles = {
            'create': auction_create_role,
            'edit_active.tendering': (blacklist('enquiryPeriod',
                                                'tenderPeriod',
                                                'rectificationPeriod',
                                                'auction_value',
                                                'auction_minimalStep',
                                                'auction_guarantee',
                                                'eligibilityCriteria',
                                                'eligibilityCriteria_en',
                                                'eligibilityCriteria_ru',
                                                'minNumberOfQualifiedBids') + edit_role),
            'Administrator': (whitelist('rectificationPeriod') + Administrator_role),
        }

    def __local_roles__(self):
        roles = dict([('{}_{}'.format(self.owner, self.owner_token), 'auction_owner')])
        for i in self.bids:
            roles['{}_{}'.format(i.owner, i.owner_token)] = 'bid_owner'
        return roles

    _internal_type = "landlease"

    auctionPeriod = ModelType(AuctionAuctionPeriod, required=True, default={})
    auctionParameters = ModelType(AuctionParameters)
    awardCriteria = StringType(choices=['highestCost'],
                               default='highestCost')                           # Specify the selection criteria, by lowest cost,

    awards = ListType(ModelType(Award), default=list())

    bids = ListType(ModelType(Bid), default=list())                             # A list of all the companies who entered submissions for the auction.

    bankAccount = ModelType(BankAccount)

    budgetSpent = ModelType(Value, required=True)

    cancellations = ListType(ModelType(Cancellation), default=list())

    complaints = ListType(ModelType(dgfCDB2Complaint), default=list())

    contracts = ListType(ModelType(Contract), default=list())

    contractTerms = ModelType(ContractTerms,
                              required=True)

    description = StringType(required=True)

    documents = ListType(ModelType(dgfCDB2Document), default=list())            # All documents and attachments related to the auction.

    enquiryPeriod = ModelType(Period)                                           # The period during which enquiries may be made and will be answered.

    guarantee = ModelType(Guarantee, required=True)

    items = ListType(ModelType(dgfCDB2Item),
                     required=True,
                     min_size=1,
                     validators=[validate_items_uniq])

    lotIdentifier = StringType(required=True)                                   # The external identifier of the lot on which this procedure is carried out

    lots = ListType(ModelType(Lot),
                    default=list(),
                    validators=[validate_lots_uniq, validate_not_available])

    lotHolder = ModelType(BaseOrganization, required=True)

    minNumberOfQualifiedBids = IntType(choices=[1, 2], required=True)

    mode = StringType()

    procurementMethod = StringType(choices=['open'], default='open')

    procurementMethodType = StringType()

    procurementMethodType = StringType(required=True)

    rectificationPeriod = ModelType(RectificationPeriod)                        # The period during which editing of main procedure fields are allowed

    registrationFee = ModelType(Value, required=True)

    status = StringType(choices=AUCTION_STATUSES, default='draft')

    submissionMethod = StringType(choices=['electronicAuction'],
                                  default='electronicAuction')

    tenderAttempts = IntType(required=True, choices=range(1, 11))

    tenderPeriod = ModelType(Period)                                            # The period when the auction is open for submissions. The end date is the closing date for auction submissions.

    def __acl__(self):
        return [
            (Allow, '{}_{}'.format(self.owner, self.owner_token), 'edit_auction'),
            (Allow, '{}_{}'.format(self.owner, self.owner_token), 'edit_auction_award'),
            (Allow, '{}_{}'.format(self.owner, self.owner_token), 'upload_auction_documents'),
        ]

    def initialize(self):
        if not self.enquiryPeriod:
            self.enquiryPeriod = type(self).enquiryPeriod.model_class()
        if not self.tenderPeriod:
            self.tenderPeriod = type(self).tenderPeriod.model_class()
        now = get_now()
        start_date = TZ.localize(self.auctionPeriod.startDate.replace(tzinfo=None))
        self.tenderPeriod.startDate = self.enquiryPeriod.startDate = now
        pause_between_periods = start_date - (start_date.replace(hour=20, minute=0, second=0, microsecond=0) - timedelta(days=1))
        end_date = calculate_business_date(start_date, -pause_between_periods, self)
        self.enquiryPeriod.endDate = end_date
        self.tenderPeriod.endDate = self.enquiryPeriod.endDate
        if not self.rectificationPeriod:
            self.rectificationPeriod = generate_rectificationPeriod(self)
        self.rectificationPeriod.startDate = now
        self.auctionPeriod.startDate = None
        self.auctionPeriod.endDate = None
        self.date = now
        if self.lots:
            for lot in self.lots:
                lot.date = now

    def validate_tenderPeriod(self, data, period):
        """Auction start date must be not closer than MINIMAL_EXPOSITION_PERIOD days and not a holiday"""
        if not (period and period.startDate and period.endDate):
            return
        if get_auction_creation_date(data) < MINIMAL_EXPOSITION_REQUIRED_FROM:
            return
        if calculate_business_date(period.startDate, MINIMAL_EXPOSITION_PERIOD, data) > period.endDate:
            raise ValidationError(u"tenderPeriod should be greater than 6 days")

    def validate_rectificationPeriod(self, data, period):
        if not (period and period.startDate) or not period.endDate:
            return
        if period.endDate > TZ.localize(calculate_business_date(data['tenderPeriod']['endDate'], -MINIMAL_PERIOD_FROM_RECTIFICATION_END, data).replace(tzinfo=None)):
            raise ValidationError(u"rectificationPeriod.endDate should come at least 5 working days earlier than tenderPeriod.endDate")

    def validate_value(self, data, value):
        if value.currency != u'UAH':
            raise ValidationError(u"currency should be only UAH")

    @serializable(serialize_when_none=False)
    def next_check(self):
        now = get_now()
        checks = []
        if self.status == 'active.tendering' and self.tenderPeriod and self.tenderPeriod.endDate:
            checks.append(self.tenderPeriod.endDate.astimezone(TZ))
        elif not self.lots and self.status == 'active.auction' and self.auctionPeriod and self.auctionPeriod.startDate and not self.auctionPeriod.endDate:
            if now < self.auctionPeriod.startDate:
                checks.append(self.auctionPeriod.startDate.astimezone(TZ))
            elif now < calc_auction_end_time(self.numberOfBids, self.auctionPeriod.startDate).astimezone(TZ):
                checks.append(calc_auction_end_time(self.numberOfBids, self.auctionPeriod.startDate).astimezone(TZ))
        elif self.lots and self.status == 'active.auction':
            for lot in self.lots:
                if lot.status != 'active' or not lot.auctionPeriod or not lot.auctionPeriod.startDate or lot.auctionPeriod.endDate:
                    continue
                if now < lot.auctionPeriod.startDate:
                    checks.append(lot.auctionPeriod.startDate.astimezone(TZ))
                elif now < calc_auction_end_time(lot.numberOfBids, lot.auctionPeriod.startDate).astimezone(TZ):
                    checks.append(calc_auction_end_time(lot.numberOfBids, lot.auctionPeriod.startDate).astimezone(TZ))
        # Use next_check part from awarding
        request = get_request_from_root(self)
        if request is not None:
            awarding_check = request.registry.getAdapter(self, IAwardingNextCheck).add_awarding_checks(self)
            if awarding_check is not None:
                checks.append(awarding_check)
        if self.status.startswith('active'):
            from openprocurement.auctions.core.utils import calculate_business_date
            for complaint in self.complaints:
                if complaint.status == 'claim' and complaint.dateSubmitted:
                    checks.append(calculate_business_date(complaint.dateSubmitted, COMPLAINT_STAND_STILL_TIME, self))
                elif complaint.status == 'answered' and complaint.dateAnswered:
                    checks.append(calculate_business_date(complaint.dateAnswered, COMPLAINT_STAND_STILL_TIME, self))
            for award in self.awards:
                for complaint in award.complaints:
                    if complaint.status == 'claim' and complaint.dateSubmitted:
                        checks.append(calculate_business_date(complaint.dateSubmitted, COMPLAINT_STAND_STILL_TIME, self))
                    elif complaint.status == 'answered' and complaint.dateAnswered:
                        checks.append(calculate_business_date(complaint.dateAnswered, COMPLAINT_STAND_STILL_TIME, self))
        return min(checks).isoformat() if checks else None


LandLease = Auction
