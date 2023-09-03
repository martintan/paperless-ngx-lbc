import itertools
import json
import logging
import os
import re
import tempfile
import urllib
import zipfile
from datetime import datetime
from pathlib import Path
from time import mktime
from unicodedata import normalize
from urllib.parse import quote

import pathvalidate
from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Case
from django.db.models import Count
from django.db.models import IntegerField
from django.db.models import Max
from django.db.models import Sum
from django.db.models import When
from django.db.models.functions import Length
from django.db.models.functions import Lower
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils.translation import get_language
from django.views.decorators.cache import cache_control
from django.views.generic import TemplateView
from django_filters.rest_framework import DjangoFilterBackend
from documents.filters import ObjectOwnedOrGrantedPermissionsFilter
from documents.permissions import PaperlessAdminPermissions
from documents.permissions import PaperlessObjectPermissions
from documents.tasks import consume_file
from langdetect import detect
from packaging import version as packaging_version
from paperless import version
from paperless.db import GnuPG
from paperless.views import StandardPagination
from rest_framework import parsers
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.filters import OrderingFilter
from rest_framework.filters import SearchFilter
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import CreateModelMixin
from rest_framework.mixins import DestroyModelMixin
from rest_framework.mixins import ListModelMixin
from rest_framework.mixins import RetrieveModelMixin
from rest_framework.mixins import UpdateModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet
from rest_framework.viewsets import ModelViewSet
from rest_framework.viewsets import ReadOnlyModelViewSet
from rest_framework.viewsets import ViewSet

from .bulk_download import ArchiveOnlyStrategy
from .bulk_download import OriginalAndArchiveStrategy
from .bulk_download import OriginalsOnlyStrategy
from .classifier import load_classifier
from .data_models import ConsumableDocument
from .data_models import DocumentMetadataOverrides
from .data_models import DocumentSource
from .filters import CorrespondentFilterSet
from .filters import DocumentFilterSet
from .filters import DocumentTypeFilterSet
from .filters import StoragePathFilterSet
from .filters import TagFilterSet
from .matching import match_correspondents
from .matching import match_document_types
from .matching import match_storage_paths
from .matching import match_tags
from .models import Correspondent, Metadata
from .models import Document
from .models import DocumentType
from .models import Note
from .models import PaperlessTask
from .models import SavedView
from .models import StoragePath
from .models import Tag
from .parsers import get_parser_class_for_mime_type
from .parsers import parse_date_generator
from .serialisers import AcknowledgeTasksViewSerializer
from .serialisers import BulkDownloadSerializer
from .serialisers import BulkEditSerializer
from .serialisers import CorrespondentSerializer
from .serialisers import DocumentListSerializer
from .serialisers import DocumentSerializer
from .serialisers import DocumentTypeSerializer
from .serialisers import PostDocumentSerializer
from .serialisers import SavedViewSerializer
from .serialisers import StoragePathSerializer
from .serialisers import TagSerializer
from .serialisers import TagSerializerVersion1
from .serialisers import TasksViewSerializer
from .serialisers import UiSettingsViewSerializer

logger = logging.getLogger("paperless.api")


class IndexView(TemplateView):
    template_name = "index.html"

    def get_frontend_language(self):
        if hasattr(
            self.request.user,
            "ui_settings",
        ) and self.request.user.ui_settings.settings.get("language"):
            lang = self.request.user.ui_settings.settings.get("language")
        else:
            lang = get_language()
        # This is here for the following reason:
        # Django identifies languages in the form "en-us"
        # However, angular generates locales as "en-US".
        # this translates between these two forms.
        if "-" in lang:
            first = lang[: lang.index("-")]
            second = lang[lang.index("-") + 1 :]
            return f"{first}-{second.upper()}"
        else:
            return lang

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["cookie_prefix"] = settings.COOKIE_PREFIX
        context["username"] = self.request.user.username
        context["full_name"] = self.request.user.get_full_name()
        context["styles_css"] = f"frontend/{self.get_frontend_language()}/styles.css"
        context["runtime_js"] = f"frontend/{self.get_frontend_language()}/runtime.js"
        context[
            "polyfills_js"
        ] = f"frontend/{self.get_frontend_language()}/polyfills.js"
        context["main_js"] = f"frontend/{self.get_frontend_language()}/main.js"
        context[
            "webmanifest"
        ] = f"frontend/{self.get_frontend_language()}/manifest.webmanifest"  # noqa: E501
        context[
            "apple_touch_icon"
        ] = f"frontend/{self.get_frontend_language()}/apple-touch-icon.png"  # noqa: E501
        return context


class PassUserMixin(CreateModelMixin):
    """
    Pass a user object to serializer
    """

    def get_serializer(self, *args, **kwargs):
        kwargs.setdefault("user", self.request.user)
        return super().get_serializer(*args, **kwargs)


class CorrespondentViewSet(ModelViewSet, PassUserMixin):
    model = Correspondent

    queryset = Correspondent.objects.annotate(
        document_count=Count("documents"),
        last_correspondence=Max("documents__created"),
    ).order_by(Lower("name"))

    serializer_class = CorrespondentSerializer
    pagination_class = StandardPagination
    permission_classes = (IsAuthenticated, PaperlessObjectPermissions)
    filter_backends = (
        DjangoFilterBackend,
        OrderingFilter,
        ObjectOwnedOrGrantedPermissionsFilter,
    )
    filterset_class = CorrespondentFilterSet
    ordering_fields = (
        "name",
        "matching_algorithm",
        "match",
        "document_count",
        "last_correspondence",
    )


class TagViewSet(ModelViewSet, PassUserMixin):
    model = Tag

    queryset = Tag.objects.annotate(document_count=Count("documents")).order_by(
        Lower("name"),
    )

    def get_serializer_class(self, *args, **kwargs):
        print(self.request.version)
        if int(self.request.version) == 1:
            return TagSerializerVersion1
        else:
            return TagSerializer

    pagination_class = StandardPagination
    permission_classes = (IsAuthenticated, PaperlessObjectPermissions)
    filter_backends = (
        DjangoFilterBackend,
        OrderingFilter,
        ObjectOwnedOrGrantedPermissionsFilter,
    )
    filterset_class = TagFilterSet
    ordering_fields = ("color", "name", "matching_algorithm", "match", "document_count")


class DocumentTypeViewSet(ModelViewSet, PassUserMixin):
    model = DocumentType

    queryset = DocumentType.objects.annotate(
        document_count=Count("documents"),
    ).order_by(Lower("name"))

    serializer_class = DocumentTypeSerializer
    pagination_class = StandardPagination
    permission_classes = (IsAuthenticated, PaperlessObjectPermissions)
    filter_backends = (
        DjangoFilterBackend,
        OrderingFilter,
        ObjectOwnedOrGrantedPermissionsFilter,
    )
    filterset_class = DocumentTypeFilterSet
    ordering_fields = ("name", "matching_algorithm", "match", "document_count")


class DocumentViewSet(
    PassUserMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    ListModelMixin,
    GenericViewSet,
):
    model = Document
    queryset = Document.objects.annotate(num_notes=Count("notes"))
    serializer_class = DocumentSerializer
    pagination_class = StandardPagination
    permission_classes = (IsAuthenticated, PaperlessObjectPermissions)
    filter_backends = (
        DjangoFilterBackend,
        SearchFilter,
        OrderingFilter,
        ObjectOwnedOrGrantedPermissionsFilter,
    )
    filterset_class = DocumentFilterSet
    search_fields = ("title", "correspondent__name", "content")
    ordering_fields = (
        "id",
        "title",
        "correspondent__name",
        "document_type__name",
        "created",
        "modified",
        "added",
        "archive_serial_number",
        "num_notes",
    )

    def get_queryset(self):
        return Document.objects.distinct().annotate(num_notes=Count("notes"))

    def get_serializer(self, *args, **kwargs):
        super().get_serializer(*args, **kwargs)
        fields_param = self.request.query_params.get("fields", None)
        fields = fields_param.split(",") if fields_param else None
        truncate_content = self.request.query_params.get("truncate_content", "False")
        serializer_class = self.get_serializer_class()
        kwargs.setdefault("context", self.get_serializer_context())
        kwargs.setdefault("fields", fields)
        kwargs.setdefault("truncate_content", truncate_content.lower() in ["true", "1"])
        return serializer_class(*args, **kwargs)

    def update(self, request, *args, **kwargs):
        response = super().update(request, *args, **kwargs)
        from documents import index

        index.add_or_update_document(self.get_object())
        return response

    def destroy(self, request, *args, **kwargs):
        from documents import index

        index.remove_document_from_index(self.get_object())
        return super().destroy(request, *args, **kwargs)

    @staticmethod
    def original_requested(request):
        return (
            "original" in request.query_params
            and request.query_params["original"] == "true"
        )

    def file_response(self, pk, request, disposition):
        doc = Document.objects.get(id=pk)
        if not self.original_requested(request) and doc.has_archive_version:
            file_handle = doc.archive_file
            filename = doc.get_public_filename(archive=True)
            mime_type = "application/pdf"
        else:
            file_handle = doc.source_file
            filename = doc.get_public_filename()
            mime_type = doc.mime_type
            # Support browser previewing csv files by using text mime type
            if mime_type in {"application/csv", "text/csv"} and disposition == "inline":
                mime_type = "text/plain"

        if doc.storage_type == Document.STORAGE_TYPE_GPG:
            file_handle = GnuPG.decrypted(file_handle)

        response = HttpResponse(file_handle, content_type=mime_type)
        # Firefox is not able to handle unicode characters in filename field
        # RFC 5987 addresses this issue
        # see https://datatracker.ietf.org/doc/html/rfc5987#section-4.2
        # Chromium cannot handle commas in the filename
        filename_normalized = normalize("NFKD", filename.replace(",", "_")).encode(
            "ascii",
            "ignore",
        )
        filename_encoded = quote(filename)
        content_disposition = (
            f"{disposition}; "
            f'filename="{filename_normalized}"; '
            f"filename*=utf-8''{filename_encoded}"
        )
        response["Content-Disposition"] = content_disposition
        return response

    def get_metadata(self, file, mime_type):
        if not os.path.isfile(file):
            return None

        parser_class = get_parser_class_for_mime_type(mime_type)
        if parser_class:
            parser = parser_class(progress_callback=None, logging_group=None)

            try:
                return parser.extract_metadata(file, mime_type)
            except Exception:
                # TODO: cover GPG errors, remove later.
                return []
        else:
            return []

    def get_filesize(self, filename):
        if os.path.isfile(filename):
            return os.stat(filename).st_size
        else:
            return None

    @action(methods=["get"], detail=True)
    def metadata(self, request, pk=None):
        try:
            doc = Document.objects.get(pk=pk)
        except Document.DoesNotExist:
            raise Http404

        meta = {
            "original_checksum": doc.checksum,
            "original_size": self.get_filesize(doc.source_path),
            "original_mime_type": doc.mime_type,
            "media_filename": doc.filename,
            "has_archive_version": doc.has_archive_version,
            "original_metadata": self.get_metadata(doc.source_path, doc.mime_type),
            "archive_checksum": doc.archive_checksum,
            "archive_media_filename": doc.archive_filename,
            "original_filename": doc.original_filename,
        }

        lang = "en"
        try:
            lang = detect(doc.content)
        except Exception:
            pass
        meta["lang"] = lang

        if doc.has_archive_version:
            meta["archive_size"] = self.get_filesize(doc.archive_path)
            meta["archive_metadata"] = self.get_metadata(
                doc.archive_path,
                "application/pdf",
            )
        else:
            meta["archive_size"] = None
            meta["archive_metadata"] = None

        return Response(meta)

    @action(methods=["get"], detail=True)
    def suggestions(self, request, pk=None):
        doc = get_object_or_404(Document, pk=pk)

        classifier = load_classifier()

        gen = parse_date_generator(doc.filename, doc.content)
        dates = sorted(
            {i for i in itertools.islice(gen, settings.NUMBER_OF_SUGGESTED_DATES)},
        )

        return Response(
            {
                "correspondents": [c.id for c in match_correspondents(doc, classifier)],
                "tags": [t.id for t in match_tags(doc, classifier)],
                "document_types": [
                    dt.id for dt in match_document_types(doc, classifier)
                ],
                "storage_paths": [dt.id for dt in match_storage_paths(doc, classifier)],
                "dates": [
                    date.strftime("%Y-%m-%d") for date in dates if date is not None
                ],
            },
        )

    @action(methods=["get"], detail=True)
    def preview(self, request, pk=None):
        try:
            response = self.file_response(pk, request, "inline")
            return response
        except (FileNotFoundError, Document.DoesNotExist):
            raise Http404

    @action(methods=["get"], detail=True)
    @method_decorator(cache_control(public=False, max_age=315360000))
    def thumb(self, request, pk=None):
        try:
            doc = Document.objects.get(id=pk)
            if doc.storage_type == Document.STORAGE_TYPE_GPG:
                handle = GnuPG.decrypted(doc.thumbnail_file)
            else:
                handle = doc.thumbnail_file
            # TODO: Send ETag information and use that to send new thumbnails
            #  if available

            return HttpResponse(handle, content_type="image/webp")
        except (FileNotFoundError, Document.DoesNotExist):
            raise Http404

    @action(methods=["get"], detail=True)
    def download(self, request, pk=None):
        try:
            return self.file_response(pk, request, "attachment")
        except (FileNotFoundError, Document.DoesNotExist):
            raise Http404

    def getNotes(self, doc):
        return [
            {
                "id": c.id,
                "note": c.note,
                "created": c.created,
                "user": {
                    "id": c.user.id,
                    "username": c.user.username,
                    "first_name": c.user.first_name,
                    "last_name": c.user.last_name,
                },
            }
            for c in Note.objects.filter(document=doc).order_by("-created")
        ]

    @action(methods=["get", "post", "delete"], detail=True)
    def notes(self, request, pk=None):
        try:
            doc = Document.objects.get(pk=pk)
        except Document.DoesNotExist:
            raise Http404

        currentUser = request.user

        if request.method == "GET":
            try:
                return Response(self.getNotes(doc))
            except Exception as e:
                logger.warning(f"An error occurred retrieving notes: {str(e)}")
                return Response(
                    {"error": "Error retreiving notes, check logs for more detail."},
                )
        elif request.method == "POST":
            try:
                c = Note.objects.create(
                    document=doc,
                    note=request.data["note"],
                    user=currentUser,
                )
                c.save()

                from documents import index

                index.add_or_update_document(self.get_object())

                return Response(self.getNotes(doc))
            except Exception as e:
                logger.warning(f"An error occurred saving note: {str(e)}")
                return Response(
                    {
                        "error": "Error saving note, check logs for more detail.",
                    },
                )
        elif request.method == "DELETE":
            note = Note.objects.get(id=int(request.GET.get("id")))
            note.delete()

            from documents import index

            index.add_or_update_document(self.get_object())

            return Response(self.getNotes(doc))

        return Response(
            {
                "error": "error",
            },
        )
    
    @action(methods=["get", "post"], detail=True)
    def index_field_metadata(self, request, pk=None):
        try:
            doc = Document.objects.get(pk=pk)
        except Document.DoesNotExist:
            raise Http404
        
        currentUser = request.user

        if request.method == "GET":
            try:
                return Response(Metadata.objects.filter(document=doc).order_by("-created"))
            except Exception as e:
                logger.warning(f"An error occurred retrieving metadatas: {str(e)}")
                return Response(
                    {"error": "Error retreiving metadatas, check logs for more detail."},
                )
        elif request.method == "POST":
            try:
                c = Metadata.objects.create(
                    document=doc,
                    data=request.data["metadata"],
                    user=currentUser,
                )
                c.save()

                from documents import index

                index.add_or_update_document(self.get_object())

                return Response(str(c.data))
            except Exception as e:
                logger.warning(f"An error occurred saving metadata: {str(e)}")
                return Response(
                    {
                        "error": "Error saving metadata, check logs for more detail.",
                    },
                )


class SearchResultSerializer(DocumentSerializer, PassUserMixin):
    def to_representation(self, instance):
        doc = Document.objects.get(id=instance["id"])
        notes = ",".join(
            [str(c.note) for c in Note.objects.filter(document=instance["id"])],
        )
        r = super().to_representation(doc)
        r["__search_hit__"] = {
            "score": instance.score,
            "highlights": instance.highlights("content", text=doc.content),
            "note_highlights": instance.highlights("notes", text=notes)
            if doc
            else None,
            "rank": instance.rank,
        }

        return r


class UnifiedSearchViewSet(DocumentViewSet):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.searcher = None

    def get_serializer_class(self):
        if self._is_search_request():
            return SearchResultSerializer
        else:
            return DocumentSerializer

    def _is_search_request(self):
        return (
            "query" in self.request.query_params
            or "more_like_id" in self.request.query_params
        )

    def filter_queryset(self, queryset):
        if self._is_search_request():
            from documents import index

            if hasattr(self.request, "user"):
                # pass user to query for perms
                self.request.query_params._mutable = True
                self.request.query_params["user"] = self.request.user.id
                self.request.query_params._mutable = False

            if "query" in self.request.query_params:
                query_class = index.DelayedFullTextQuery
            elif "more_like_id" in self.request.query_params:
                query_class = index.DelayedMoreLikeThisQuery
            else:
                raise ValueError

            return query_class(
                self.searcher,
                self.request.query_params,
                self.paginator.get_page_size(self.request),
            )
        else:
            return super().filter_queryset(queryset)

    def list(self, request, *args, **kwargs):
        if self._is_search_request():
            from documents import index

            try:
                with index.open_index_searcher() as s:
                    self.searcher = s
                    return super().list(request)
            except NotFound:
                raise
            except Exception as e:
                return HttpResponseBadRequest(str(e))
        else:
            return super().list(request)


class LogViewSet(ViewSet):

    permission_classes = (IsAuthenticated, PaperlessAdminPermissions)

    log_files = ["paperless", "mail"]

    def get_log_filename(self, log):
        return os.path.join(settings.LOGGING_DIR, f"{log}.log")

    def retrieve(self, request, pk=None, *args, **kwargs):
        if pk not in self.log_files:
            raise Http404

        filename = self.get_log_filename(pk)

        if not os.path.isfile(filename):
            raise Http404

        with open(filename) as f:
            lines = [line.rstrip() for line in f.readlines()]

        return Response(lines)

    def list(self, request, *args, **kwargs):
        exist = [
            log for log in self.log_files if os.path.isfile(self.get_log_filename(log))
        ]
        return Response(exist)


class SavedViewViewSet(ModelViewSet, PassUserMixin):
    model = SavedView

    queryset = SavedView.objects.all()
    serializer_class = SavedViewSerializer
    pagination_class = StandardPagination
    permission_classes = (IsAuthenticated, PaperlessObjectPermissions)

    def get_queryset(self):
        user = self.request.user
        return SavedView.objects.filter(owner=user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class BulkEditView(GenericAPIView):

    permission_classes = (IsAuthenticated,)
    serializer_class = BulkEditSerializer
    parser_classes = (parsers.JSONParser,)

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        method = serializer.validated_data.get("method")
        parameters = serializer.validated_data.get("parameters")
        documents = serializer.validated_data.get("documents")

        try:
            # TODO: parameter validation
            result = method(documents, **parameters)
            return Response({"result": result})
        except Exception as e:
            return HttpResponseBadRequest(str(e))


class PostDocumentView(GenericAPIView):

    permission_classes = (IsAuthenticated,)
    serializer_class = PostDocumentSerializer
    parser_classes = (parsers.MultiPartParser,)

    def post(self, request, *args, **kwargs):

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        doc_name, doc_data = serializer.validated_data.get("document")
        correspondent_id = serializer.validated_data.get("correspondent")
        document_type_id = serializer.validated_data.get("document_type")
        tag_ids = serializer.validated_data.get("tags")
        title = serializer.validated_data.get("title")
        created = serializer.validated_data.get("created")
        archive_serial_number = serializer.validated_data.get("archive_serial_number")
        storage_path_id = serializer.validated_data.get("storage_path_id")
        full_path = serializer.validated_data.get("full_path")
        is_large_file = serializer.validated_data.get("is_large_file")
        ocr_specific_pages = serializer.validated_data.get("ocr_specific_pages")

        logger.debug(f"storage_path_id: {storage_path_id}")

        t = int(mktime(datetime.now().timetuple()))

        os.makedirs(settings.SCRATCH_DIR, exist_ok=True)

        temp_file_path = Path(tempfile.mkdtemp(dir=settings.SCRATCH_DIR)) / Path(
            pathvalidate.sanitize_filename(doc_name),
        )

        temp_file_path.write_bytes(doc_data)

        os.utime(temp_file_path, times=(t, t))

        input_doc = ConsumableDocument(
            source=DocumentSource.ApiUpload,
            original_file=temp_file_path,
        )
        input_doc_overrides = DocumentMetadataOverrides(
            filename=doc_name,
            title=title,
            correspondent_id=correspondent_id,
            document_type_id=document_type_id,
            tag_ids=tag_ids,
            created=created,
            asn=archive_serial_number,
            # Intentionally removing this because it prevents other users from editing
            # owner_id=request.user.id,
            storage_path_id=storage_path_id,
            full_path=full_path,
            is_large_file=is_large_file,
            ocr_specific_pages=ocr_specific_pages
        )

        async_task = consume_file.delay(
            input_doc,
            input_doc_overrides,
        )

        return Response(async_task.id)


class SelectionDataView(GenericAPIView):

    permission_classes = (IsAuthenticated,)
    serializer_class = DocumentListSerializer
    parser_classes = (parsers.MultiPartParser, parsers.JSONParser)

    def post(self, request, format=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        ids = serializer.validated_data.get("documents")

        correspondents = Correspondent.objects.annotate(
            document_count=Count(
                Case(When(documents__id__in=ids, then=1), output_field=IntegerField()),
            ),
        )

        tags = Tag.objects.annotate(
            document_count=Count(
                Case(When(documents__id__in=ids, then=1), output_field=IntegerField()),
            ),
        )

        types = DocumentType.objects.annotate(
            document_count=Count(
                Case(When(documents__id__in=ids, then=1), output_field=IntegerField()),
            ),
        )

        storage_paths = StoragePath.objects.annotate(
            document_count=Count(
                Case(When(documents__id__in=ids, then=1), output_field=IntegerField()),
            ),
        )

        r = Response(
            {
                "selected_correspondents": [
                    {"id": t.id, "document_count": t.document_count}
                    for t in correspondents
                ],
                "selected_tags": [
                    {"id": t.id, "document_count": t.document_count} for t in tags
                ],
                "selected_document_types": [
                    {"id": t.id, "document_count": t.document_count} for t in types
                ],
                "selected_storage_paths": [
                    {"id": t.id, "document_count": t.document_count}
                    for t in storage_paths
                ],
            },
        )

        return r


class SearchAutoCompleteView(APIView):

    permission_classes = (IsAuthenticated,)

    def get(self, request, format=None):
        if "term" in request.query_params:
            term = request.query_params["term"]
        else:
            return HttpResponseBadRequest("Term required")

        if "limit" in request.query_params:
            limit = int(request.query_params["limit"])
            if limit <= 0:
                return HttpResponseBadRequest("Invalid limit")
        else:
            limit = 10

        from documents import index

        ix = index.open_index()

        return Response(index.autocomplete(ix, term, limit))


class StatisticsView(APIView):

    permission_classes = (IsAuthenticated,)

    def get(self, request, format=None):
        documents_total = Document.objects.all().count()

        inbox_tag = Tag.objects.filter(is_inbox_tag=True)

        documents_inbox = (
            Document.objects.filter(tags__is_inbox_tag=True).distinct().count()
            if inbox_tag.exists()
            else None
        )

        document_file_type_counts = (
            Document.objects.values("mime_type")
            .annotate(mime_type_count=Count("mime_type"))
            .order_by("-mime_type_count")
            if documents_total > 0
            else 0
        )

        character_count = (
            Document.objects.annotate(
                characters=Length("content"),
            )
            .aggregate(Sum("characters"))
            .get("characters__sum")
        )

        return Response(
            {
                "documents_total": documents_total,
                "documents_inbox": documents_inbox,
                "inbox_tag": inbox_tag.first().pk if inbox_tag.exists() else None,
                "document_file_type_counts": document_file_type_counts,
                "character_count": character_count,
            },
        )


class BulkDownloadView(GenericAPIView):

    permission_classes = (IsAuthenticated,)
    serializer_class = BulkDownloadSerializer
    parser_classes = (parsers.JSONParser,)

    def post(self, request, format=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        ids = serializer.validated_data.get("documents")
        compression = serializer.validated_data.get("compression")
        content = serializer.validated_data.get("content")
        follow_filename_format = serializer.validated_data.get("follow_formatting")

        os.makedirs(settings.SCRATCH_DIR, exist_ok=True)
        temp = tempfile.NamedTemporaryFile(
            dir=settings.SCRATCH_DIR,
            suffix="-compressed-archive",
            delete=False,
        )

        if content == "both":
            strategy_class = OriginalAndArchiveStrategy
        elif content == "originals":
            strategy_class = OriginalsOnlyStrategy
        else:
            strategy_class = ArchiveOnlyStrategy

        with zipfile.ZipFile(temp.name, "w", compression) as zipf:
            strategy = strategy_class(zipf, follow_filename_format)
            for id in ids:
                doc = Document.objects.get(id=id)
                strategy.add_document(doc)

        with open(temp.name, "rb") as f:
            response = HttpResponse(f, content_type="application/zip")
            response["Content-Disposition"] = '{}; filename="{}"'.format(
                "attachment",
                "documents.zip",
            )

            return response


class RemoteVersionView(GenericAPIView):
    def get(self, request, format=None):
        remote_version = "0.0.0"
        is_greater_than_current = False
        current_version = packaging_version.parse(version.__full_version_str__)
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/paperless-ngx/"
                "paperless-ngx/releases/latest",
            )
            # Ensure a JSON response
            req.add_header("Accept", "application/json")

            with urllib.request.urlopen(req) as response:
                remote = response.read().decode("utf-8")
            try:
                remote_json = json.loads(remote)
                remote_version = remote_json["tag_name"]
                # Basically PEP 616 but that only went in 3.9
                if remote_version.startswith("ngx-"):
                    remote_version = remote_version[len("ngx-") :]
            except ValueError:
                logger.debug("An error occurred parsing remote version json")
        except urllib.error.URLError:
            logger.debug("An error occurred checking for available updates")

        is_greater_than_current = (
            packaging_version.parse(
                remote_version,
            )
            > current_version
        )

        return Response(
            {
                "version": remote_version,
                "update_available": is_greater_than_current,
            },
        )


class StoragePathViewSet(ModelViewSet, PassUserMixin):
    model = StoragePath

    queryset = StoragePath.objects.annotate(document_count=Count("documents")).order_by(
        Lower("name"),
    )

    serializer_class = StoragePathSerializer
    pagination_class = StandardPagination
    permission_classes = (IsAuthenticated, PaperlessObjectPermissions)
    filter_backends = (DjangoFilterBackend, OrderingFilter)
    filterset_class = StoragePathFilterSet
    ordering_fields = ("name", "path", "matching_algorithm", "match", "document_count")


class FilesAndFoldersViewSet(ReadOnlyModelViewSet):
    permission_classes = (IsAuthenticated,)

    def list(self, request):
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 100))
        ordering = request.query_params.get('ordering', '-created')
        parent_storage_path_id = request.query_params.get('parent_storage_path_id', None)
        parent_storage_path = None
        
        # parent_folder = request.query_params.get('path__istartswith', '')
        if parent_storage_path_id:
            parent_storage_path = StoragePath.objects.get(id=parent_storage_path_id)
            folders = list(StoragePath.objects
                           .filter(path__istartswith=parent_storage_path.path)
                           .exclude(id=parent_storage_path.id))
            files = list(Document.objects.all().filter(storage_path=parent_storage_path).order_by(ordering))
        else:
            folders = list(StoragePath.objects.exclude(path__contains='/'))
            files = list(Document.objects.all().filter(storage_path=None).order_by(ordering))

        combined = folders + files
        
        start = (page - 1) * page_size
        end = page * page_size
        sliced_combined = combined[start:end]

        data = []
        for item in sliced_combined:
            if isinstance(item, StoragePath):
                serialized_item = StoragePathSerializer(item).data
                serialized_item['type'] = 'folder'
                data.append(serialized_item)
            elif isinstance(item, Document):
                serialized_item = DocumentSerializer(item).data
                serialized_item['type'] = 'file'
                data.append(serialized_item)

        return Response({
            'count': len(combined),
            'next': None,
            'previous': None,
            'results': data,
            'parentStoragePath': StoragePathSerializer(parent_storage_path).data if parent_storage_path else None
        })

class UiSettingsView(GenericAPIView):

    permission_classes = (IsAuthenticated,)
    serializer_class = UiSettingsViewSerializer

    def get(self, request, format=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = User.objects.get(pk=request.user.id)
        ui_settings = {}
        if hasattr(user, "ui_settings"):
            ui_settings = user.ui_settings.settings
        if "update_checking" in ui_settings:
            ui_settings["update_checking"][
                "backend_setting"
            ] = settings.ENABLE_UPDATE_CHECK
        else:
            ui_settings["update_checking"] = {
                "backend_setting": settings.ENABLE_UPDATE_CHECK,
            }
        # strip <app_label>.
        roles = map(lambda perm: re.sub(r"^\w+.", "", perm), user.get_all_permissions())
        return Response(
            {
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "is_superuser": user.is_superuser,
                    "groups": user.groups.values_list("id", flat=True),
                },
                "settings": ui_settings,
                "permissions": roles,
            },
        )

    def post(self, request, format=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        serializer.save(user=self.request.user)

        return Response(
            {
                "success": True,
            },
        )


class TasksViewSet(ReadOnlyModelViewSet):

    permission_classes = (IsAuthenticated,)
    serializer_class = TasksViewSerializer

    def get_queryset(self):
        queryset = (
            PaperlessTask.objects.filter(
                acknowledged=False,
            )
            .order_by("date_created")
            .reverse()
        )
        task_id = self.request.query_params.get("task_id")
        if task_id is not None:
            queryset = PaperlessTask.objects.filter(task_id=task_id)
        return queryset


class AcknowledgeTasksView(GenericAPIView):

    permission_classes = (IsAuthenticated,)
    serializer_class = AcknowledgeTasksViewSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tasks = serializer.validated_data.get("tasks")

        try:
            result = PaperlessTask.objects.filter(id__in=tasks).update(
                acknowledged=True,
            )
            return Response({"result": result})
        except Exception:
            return HttpResponseBadRequest()
