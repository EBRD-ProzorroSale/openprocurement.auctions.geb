# -*- coding: utf-8 -*-
from openprocurement.auctions.core.utils import (
    json_view,
    context_unpack,
    save_auction,
    apply_patch,
    opresource,
)
from openprocurement.auctions.core.validation import (
    validate_file_update,
    validate_file_upload,
    validate_patch_document_data,
)
from openprocurement.auctions.core.views.mixins import AuctionDocumentResource

from openprocurement.auctions.core.interfaces import (
    IAuctionManager
)

from openprocurement.auctions.landlease.interfaces import (
    IAuctionDocumenter
)
from openprocurement.auctions.landlease.utils import (
    upload_file, get_file, invalidate_bids_data
)

from openprocurement.auctions.landlease.constants import (
    PROCEDURE_DOCUMENT_STATUSES,
    AUCTION_DOCUMENT_STATUSES
)


@opresource(name='landlease:Auction Documents',
            collection_path='/auctions/{auction_id}/documents',
            path='/auctions/{auction_id}/documents/{document_id}',
            auctionsprocurementMethodType="landlease",
            description="Auction related binary files (PDFs, etc.)")
class AuctionDocumentResource(AuctionDocumentResource):

    def validate_document_editing_period(self, operation):
        status = self.request.validated['auction_status']
        role = self.request.authenticated_role

        if role != 'auction':
            auction_not_in_editable_state = status not in PROCEDURE_DOCUMENT_STATUSES
        else:
            auction_not_in_editable_state = status not in AUCTION_DOCUMENT_STATUSES

        if auction_not_in_editable_state:
            err_msg = 'Can\'t {} document in current ({}) auction status'.format(operation, status)
            self.request.errors.add('body', 'data', err_msg)
            self.request.errors.status = 403
            return
        return True

    @json_view(permission='upload_auction_documents', validators=(validate_file_upload,))
    def collection_post(self):
        """Auction Document Upload"""
        save = None
        manager = self.request.registry.queryMultiAdapter((self.request, self.context), IAuctionManager)

        documenter = self.request.registry.queryMultiAdapter((self.request, self.context), IAuctionDocumenter)
        document = manager.upload_document(documenter)
        if document:
            save = manager.save()
        if save:
            msg = 'Created auction document {}'.format(document.id)
            extra = context_unpack(self.request, {'MESSAGE_ID': 'auction_document_create'}, {'document_id': document['id']})
            self.LOGGER.info(msg, extra=extra)

            self.request.response.status = 201

            document_route = self.request.matched_route.name.replace("collection_", "")
            locations = self.request.current_route_url(_route_name=document_route, document_id=document.id, _query={})
            self.request.response.headers['Location'] = locations

            return {'data': document.serialize("view")}

    @json_view(permission='view_auction')
    def get(self):
        """Auction Document Read"""
        document = self.request.validated['document']
        offline = bool(document.get('documentType') == 'x_dgfAssetFamiliarization')
        if self.request.params.get('download') and not offline:
            return get_file(self.request)
        document_data = document.serialize("view")
        document_data['previousVersions'] = [
            i.serialize("view")
            for i in self.request.validated['documents']
            if i.url != document.url or
            (offline and i.dateModified != document.dateModified)
        ]
        return {'data': document_data}

    @json_view(permission='upload_auction_documents', validators=(validate_file_update,))
    def put(self):
        """Auction Document Update"""
        if not self.validate_document_editing_period('update'):
            return
        document = upload_file(self.request)
        if self.request.authenticated_role != "auction":
            invalidate_bids_data(self.request.auction)
        self.request.validated['auction'].documents.append(document)
        if save_auction(self.request):
            self.LOGGER.info('Updated auction document {}'.format(self.request.context.id),
                             extra=context_unpack(self.request, {'MESSAGE_ID': 'auction_document_put'}))
            return {'data': document.serialize("view")}

    @json_view(content_type="application/json", permission='upload_auction_documents', validators=(validate_patch_document_data,))
    def patch(self):
        """Auction Document Update"""
        if not self.validate_document_editing_period('update'):
            return
        apply_patch(self.request, save=False, src=self.request.context.serialize())
        if self.request.authenticated_role != "auction":
            invalidate_bids_data(self.request.auction)
        if save_auction(self.request):
            self.LOGGER.info('Updated auction document {}'.format(self.request.context.id),
                             extra=context_unpack(self.request, {'MESSAGE_ID': 'auction_document_patch'}))
            return {'data': self.request.context.serialize("view")}
