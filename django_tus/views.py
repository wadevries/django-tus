import base64
import logging
import os
import uuid

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View

from django_tus.signals import tus_upload_finished_signal

logger = logging.getLogger(__name__)


TUS_SETTINGS = {}


class TusUpload(View):
    TUS_UPLOAD_URL = getattr(settings, "TUS_UPLOAD_URL", '/media')
    TUS_UPLOAD_DIR = getattr(settings, "TUS_UPLOAD_DIR", os.path.join(settings.BASE_DIR, 'tmp/uploads/'))
    TUS_MAX_FILE_SIZE = getattr(settings, "TUS_MAX_FILE_SIZE", 4294967296)  # in bytes
    TUS_FILE_OVERWRITE = getattr(settings, "TUS_FILE_OVERWRITE", True)
    TUS_TIMEOUT = getattr(settings, "TUS_TIMEOUT", 3600)

    tus_api_version = '1.0.0'
    tus_api_version_supported = ['1.0.0']
    tus_api_extensions = ['creation', 'termination', 'file-check']
    on_finish = None

    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        override_method = request.META.get('HTTP_X_HTTP_METHOD_OVERRIDE', None)
        if override_method:
            request.method = override_method
        logger.error("TUS dispatch", extra={'requestMETA': request.META, "requestMethod": request.method})

        return super(TusUpload, self).dispatch(request, *args, **kwargs)

    def get_tus_response(self):
        response = HttpResponse()
        response['Tus-Resumable'] = self.tus_api_version
        response['Tus-Version'] = ",".join(self.tus_api_version_supported)
        response['Tus-Extension'] = ",".join(self.tus_api_extensions)
        response['Tus-Max-Size'] = self.TUS_MAX_FILE_SIZE
        response['Access-Control-Allow-Origin'] = "*"
        response['Access-Control-Allow-Methods'] = "PATCH,HEAD,GET,POST,OPTIONS"
        response['Access-Control-Expose-Headers'] = "Tus-Resumable,upload-length,upload-metadata,Location,Upload-Offset"
        response['Access-Control-Allow-Headers'] = "Tus-Resumable,upload-length,upload-metadata,Location,Upload-Offset,content-type"
        response['Cache-Control'] = 'no-store'

        return response

    def get_destination_dir(self):
        return getattr(settings, "TUS_DESTINATION_DIR", settings.MEDIA_ROOT)

    def finished(self):
        if self.on_finish is not None:
            self.on_finish()

    def get(self, request, *args, **kwargs):
        """
        :param request:
        :param args:
        :param kwargs:
        :return response:
        """

        metadata = {}
        response = self.get_tus_response()

        if request.META.get("HTTP_TUS_RESUMABLE", None) is None:
            return HttpResponse(status=405, content="Method Not Allowed")

        for kv in request.META.get("HTTP_UPLOAD_METADATA", None).split(","):
            (key, value) = kv.split(" ")
            metadata[key] = base64.b64decode(value)

        filename = metadata.get("filename", None)
        if filename and filename.upper() in [f.upper() for f in os.listdir(os.path.dirname(self.TUS_UPLOAD_DIR))]:
            response['Tus-File-Name'] = filename
            response['Tus-File-Exists'] = True
        else:
            response['Tus-File-Exists'] = False
        return response

    def options(self, request, *args, **kwargs):
        """
        :param request:
        :param args:
        :param kwargs:
        :return response:
        """

        response = self.get_tus_response()
        response.status_code = 204
        return response

    def post(self, request, *args, **kwargs):
        """
        :param request:
        :param args:
        :param kwargs:
        :return:
        """
        response = self.get_tus_response()

        if request.META.get("HTTP_TUS_RESUMABLE", None) is None:
            # handle in dispatch?
            logger.warning("Received File upload for unsupported file transfer protocol")
            response.status_code = 500
            response.reason_phrase = "Received File upload for unsupported file transfer protocol"

        if request.method == 'OPTIONS':
            response['Tus-Max-Size'] = self.TUS_MAX_FILE_SIZE
            response.status_code = 204
            return response

        metadata = {}
        upload_metadata = request.META.get("HTTP_UPLOAD_METADATA", None)

        message_id = request.META.get("HTTP_MESSAGE_ID", None)
        if message_id:
            message_id = base64.b64decode(message_id)
            metadata["message_id"] = message_id
        logger.error("TUS Request", extra={'request': request.META})

        if upload_metadata:
            for kv in upload_metadata.split(","):
                (key, value) = kv.split(" ")
                metadata[key] = base64.b64decode(value).decode("utf-8")

        user_filename = metadata.get("filename")
        if user_filename and os.path.lexists(
                os.path.join(self.TUS_UPLOAD_DIR, user_filename)) and self.TUS_FILE_OVERWRITE is False:
            response.status_code = 409
            return response
        else:
            logger.error("Unable to access file", extra={'request': request.META, 'metadata': metadata})
            # response.status_code = 409
            # return response

        file_size = int(request.META.get("HTTP_UPLOAD_LENGTH", "0"))
        resource_id = str(uuid.uuid4())

        def key(detail):
            return 'tus-uploads/{}/{}'.format(resource_id, detail)

        cache.add(key('filename'), "{}".format(user_filename), self.TUS_TIMEOUT)
        cache.add(key('file_size'), file_size, self.TUS_TIMEOUT)
        cache.add(key('offset'), 0, self.TUS_TIMEOUT)
        cache.add(key('metadata'), metadata, self.TUS_TIMEOUT)

        try:
            f = open(os.path.join(self.TUS_UPLOAD_DIR, resource_id), "wb")
            f.seek(file_size)
            f.write(b"\0")
            f.close()
        except IOError as e:
            logger.error("Unable to create file: {}".format(e), exc_info=True, extra={'request': request})
            response.status_code = 500
            return response

        response.status_code = 201
        response['Location'] = '{}{}'.format(request.build_absolute_uri(), resource_id)
        return response

    def head(self, request, resource_id, *args, **kwargs):
        response = self.get_tus_response()

        offset = cache.get("tus-uploads/{}/offset".format(resource_id))
        file_size = cache.get("tus-uploads/{}/file_size".format(resource_id))
        if offset is None:
            response.status_code = 404
            return response
        else:
            response.status_code = 200
            response['Upload-Offset'] = offset
            response['Upload-Length'] = file_size

        return response

    def patch(self, request, resource_id, *args, **kwargs):
        response = self.get_tus_response()

        filename = cache.get("tus-uploads/{}/filename".format(resource_id))
        file_size = int(cache.get("tus-uploads/{}/file_size".format(resource_id)))
        metadata = cache.get("tus-uploads/{}/metadata".format(resource_id))
        offset = cache.get("tus-uploads/{}/offset".format(resource_id))

        file_offset = int(request.META.get("HTTP_UPLOAD_OFFSET", 0))
        chunk_size = int(request.META.get("CONTENT_LENGTH", 102400))

        upload_file_path = os.path.join(self.TUS_UPLOAD_DIR, resource_id)
        if filename is None or not os.path.lexists(upload_file_path):
            response.status_code = 410
            return response

        if file_offset != offset:  # check to make sure we're in sync
            response.status_code = 409  # HTTP 409 Conflict
            return response

        logger.error("patch", extra={'request': request.META, 'tus': {
            "resource_id": resource_id,
            "filename": filename,
            "file_size": file_size,
            "metadata": metadata,
            "offset": offset,
            "upload_file_path": upload_file_path,
        }})

        try:
            file_ = open(upload_file_path, "r+b")
        except IOError:
            file_ = open(upload_file_path, "wb")
        finally:
            file_.seek(file_offset)
            file_.write(request.body)
            file_.close()

        new_offset = cache.incr("tus-uploads/{}/offset".format(resource_id), chunk_size)
        response['Upload-Offset'] = new_offset
        logger.error("pre_finish_check")
        if file_size == new_offset:  # file transfer complete, rename from resource id to actual filename
            logger.error("post_finish_check")

            filename = uuid.uuid4().hex + "_" + filename
            os.rename(upload_file_path, os.path.join(self.get_destination_dir(), filename))
            cache.delete_many([
                "tus-uploads/{}/file_size".format(resource_id),
                "tus-uploads/{}/filename".format(resource_id),
                "tus-uploads/{}/offset".format(resource_id),
                "tus-uploads/{}/metadata".format(resource_id),
            ])
            # sending signal
            tus_upload_finished_signal.send(
                sender=self.__class__,
                metadata=metadata,
                filename=filename,
                upload_file_path=upload_file_path,
                file_size=file_size,
                upload_url=self.TUS_UPLOAD_URL,
                destination_folder=self.get_destination_dir())

            self.finished()

        return response


