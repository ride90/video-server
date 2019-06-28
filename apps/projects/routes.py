import os
import re
import copy
import logging
from datetime import datetime

from bson import json_util
from flask import request, make_response
from flask import current_app as app
from pymongo import ReturnDocument
from pymongo.errors import ServerSelectionTimeoutError
from werkzeug.exceptions import BadRequest, InternalServerError, NotFound

from lib.utils import (
    create_file_name, json_response, add_urls, validate_document,
    get_request_address, save_activity_log, paginate
)
from lib.video_editor import get_video_editor
from lib.views import MethodView

from .tasks import edit_video, generate_timeline_thumbnails, generate_preview_thumbnail
from . import bp

logger = logging.getLogger(__name__)


class ListUploadProject(MethodView):
    SCHEMA_UPLOAD = {
        'file': {
            'type': 'filestorage',
            'required': True
        }
    }

    def post(self):
        """
        Create new project record in DB and save file into file storage
        ---
        consumes:
          - multipart/form-data
        parameters:
        - in: formData
          name: file
          type: file
          description: file object to upload
        responses:
          201:
            description: CREATED
            schema:
              type: object
              properties:
                filename:
                  type: string
                  example: fa5079a38e0a4197864aa2ccb07f3bea.mp4
                url:
                  type: string
                  example: https://example.com/raw/fa5079a38e0a4197864aa2ccb07f3bea4
                storage_id:
                  type: string
                  example: 2019/5/1/fa5079a38e0a4197864aa2ccb07f3bea.mp4
                metadata:
                  type: object
                  properties:
                    codec_name:
                      type: string
                      example: h264
                    codec_long_name:
                      type: string
                      example: H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
                    width:
                      type: int
                      example: 640
                    height:
                      type: int
                      example: 360
                    duration:
                      type: float
                      example: 300.014000
                    bit_rate:
                      type: int
                      example: 287654
                    nb_frames:
                      type: int
                      example: 7654
                    r_frame_rate:
                      type: string
                      example: 24/1
                    format_name:
                      type: string
                      example: mov,mp4,m4a,3gp,3g2,mj2
                    size:
                      type: int
                      example: 14567890
                mime_type:
                  type: string
                  example: video/mp4
                create_time:
                  type: string
                  example: 2019-05-01T09:00:00+00:00
                original_filename:
                  type: string
                  example: video.mp4
                request_address:
                  type: string
                  example: 127.0.0.1
                version:
                  type: integer
                  example: 1
                parent:
                  type: object
                  example: {}
                processing:
                  type: boolean
                  example: False
                thumbnails:
                  type: object
                  example: {}
                _id:
                  type: string
                  example: 5cbd5acfe24f6045607e51aa
        """

        # validate request
        if 'file' not in request.files:
            # to avoid TypeError: cannot serialize '_io.BufferedRandom' error
            raise BadRequest({"file": ["required field"]})
        document = validate_document(request.files, self.SCHEMA_UPLOAD)

        # validate codec
        file_stream = document['file'].stream.read()
        metadata = get_video_editor().get_meta(file_stream)
        if metadata.get('codec_name') not in app.config.get('CODEC_SUPPORT_VIDEO'):
            raise BadRequest(f"Codec: '{metadata.get('codec_name')}' is not supported.")

        # add record to database
        project = {
            'filename': create_file_name(ext=document['file'].filename.rsplit('.')[-1]),
            'storage_id': None,
            'metadata': metadata,
            'create_time': datetime.utcnow(),
            'mime_type': document['file'].mimetype,
            'request_address': get_request_address(request.headers.environ),
            'original_filename': document['file'].filename,
            'version': 1,
            'parent': None,
            'processing': {
                'video': False,
                'thumbnail_preview': False,
                'thumbnails_timeline': False
            },
            'thumbnails': {
                'timeline': [],
                'preview': None
            }
        }
        app.mongo.db.projects.insert_one(project)

        # put file stream into storage
        try:
            storage_id = app.fs.put(
                content=file_stream,
                filename=project['filename'],
                project_id=project['_id'],
                content_type=document['file'].mimetype
            )
        except Exception as e:
            # remove record from db
            app.mongo.db.projects.delete_one({'_id': project['_id']})
            raise InternalServerError(str(e))

        # set 'storage_id' for project
        try:
            project = app.mongo.db.projects.find_one_and_update(
                {'_id': project['_id']},
                {'$set': {'storage_id': storage_id}},
                return_document=ReturnDocument.AFTER
            )
        except ServerSelectionTimeoutError as e:
            # delete project dir
            app.fs.delete_dir(storage_id)
            # remove record from db
            app.mongo.db.projects.delete_one({'_id': project['_id']})
            raise InternalServerError(str(e))

        save_activity_log('upload', project['_id'])
        add_urls(project)

        return json_response(project, status=201)

    def get(self):
        """
        Get list of projects in DB
        ---
        parameters:
        - name: page
          in: query
          type: integer
          description: Page number
        responses:
          200:
            description: list of projects
            schema:
              type: object
              properties:
                _meta:
                  type: object
                  properties:
                    page:
                      type: integer
                      example: 1
                    max_results:
                      type: integer
                      example: 25
                    total:
                      type: integer
                      example: 230
                _items:
                  type: array
                  items:
                    type: object
                    properties:
                      filename:
                        type: string
                        example: fa5079a38e0a4197864aa2ccb07f3bea.mp4
                      url:
                        type: string
                        example: https://example.com/raw/fa5079a38e0a4197864aa2ccb07f3bea
                      storage_id:
                        type: string
                        example: 2019/5/fa5079a38e0a4197864aa2ccb07f3bea.mp4
                      metadata:
                        type: object
                        properties:
                          codec_name:
                            type: string
                            example: h264
                          codec_long_name:
                            type: string
                            example: H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
                          width:
                            type: int
                            example: 640
                          height:
                            type: int
                            example: 360
                          duration:
                            type: float
                            example: 300.014000
                          bit_rate:
                            type: int
                            example: 287654
                          nb_frames:
                            type: int
                            example: 7654
                          r_frame_rate:
                            type: string
                            example: 24/1
                          format_name:
                            type: string
                            example: mov,mp4,m4a,3gp,3g2,mj2
                          size:
                            type: int
                            example: 14567890
                      mime_type:
                        type: string
                        example: video/mp4
                      create_time:
                        type: string
                        example: 2019-05-01T09:00:00+00:00
                      original_filename:
                        type: string
                        example: video.mp4
                      request_address:
                        type: string
                        example: 127.0.0.1
                      version:
                        type: integer
                        example: 1
                      parent:
                        type: object
                        example: {}
                      processing:
                        type: boolean
                        example: False
                      thumbnails:
                        type: object
                        example: {}
                      _id:
                        type: string
                        example: 5cbd5acfe24f6045607e51aa
        """

        page = request.args.get('page', 1, type=int)
        projects = list(paginate(
            cursor=app.mongo.db.projects.find(),
            page=page
        ))
        add_urls(projects)

        return json_response(
            {
                '_items': projects,
                '_meta': {
                    'page': page,
                    'max_results': app.config.get('ITEMS_PER_PAGE'),
                    'total': app.mongo.db.projects.count()
                }
            }
        )


class RetrieveEditDestroyProject(MethodView):

    @property
    def schema_edit(self):
        return {
            'trim': {
                'type': 'dict',
                'required': False,
                'schema': {
                    'start': {
                        'type': 'float',
                        'min': 0,
                        'required': True
                    },
                    'end': {
                        'type': 'float',
                        'min': 1,
                        'required': True
                    },
                }
            },
            'rotate': {
                'type': 'integer',
                'required': False,
                'allowed': [-270, -180, -90, 90, 180, 270]
            },
            'scale': {
                'type': 'integer',
                'min': app.config.get('MIN_VIDEO_WIDTH'),
                'max': app.config.get('MAX_VIDEO_WIDTH'),
                'required': False
            },
            'crop': {
                'type': 'dict',
                'required': False,
                'empty': True,
                'schema': {
                    'width': {
                        'type': 'integer',
                        'min': app.config.get('MIN_VIDEO_WIDTH'),
                        'max': app.config.get('MAX_VIDEO_WIDTH'),
                        'required': True
                    },
                    'height': {
                        'type': 'integer',
                        'min': app.config.get('MIN_VIDEO_HEIGHT'),
                        'max': app.config.get('MAX_VIDEO_HEIGHT'),
                        'required': True
                    },
                    'x': {
                        'type': 'integer',
                        'required': True,
                        'min': 0
                    },
                    'y': {
                        'type': 'integer',
                        'required': True,
                        'min': 0
                    }
                }
            }
        }

    def get(self, project_id):
        """
        Retrieve project details
        ---
        parameters:
            - name: project_id
              in: path
              type: string
              required: true
              description: Unique project id
        responses:
          200:
            description: OK
            schema:
              type: object
              properties:
                filename:
                  type: string
                  example: fa5079a38e0a4197864aa2ccb07f3bea.mp4
                url:
                  type: string
                  example: https://example.com/raw/fa5079a38e0a4197864aa2ccb07f3bea
                storage_id:
                  type: string
                  example: 2019/5/fa5079a38e0a4197864aa2ccb07f3bea.mp4
                metadata:
                  type: object
                  properties:
                    codec_name:
                      type: string
                      example: h264
                    codec_long_name:
                      type: string
                      example: H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
                    width:
                      type: integer
                      example: 640
                    height:
                      type: integer
                      example: 360
                    duration:
                      type: float
                      example: 300.014000
                    bit_rate:
                      type: int
                      example: 287654
                    nb_frames:
                      type: int
                      example: 7654
                    r_frame_rate:
                      type: string
                      example: 24/1
                    format_name:
                      type: string
                      example: mov,mp4,m4a,3gp,3g2,mj2
                    size:
                      type: int
                      example: 14567890
                mime_type:
                  type: string
                  example: video/mp4
                create_time:
                  type: string
                  example: 2019-05-01T09:00:00+00:00
                original_filename:
                  type: string
                  example: video.mp4
                request_address:
                  type: string
                  example: 127.0.0.1
                version:
                  type: integer
                  example: 1
                parent:
                  type: object
                  example: {}
                processing:
                  type: boolean
                  example: False
                thumbnails:
                  type: object
                  example: {}
                _id:
                  type: string
                  example: 5cbd5acfe24f6045607e51aa
        """

        add_urls(self._project)
        return json_response(self._project)

    def put(self, project_id):
        """
        Edit video. This method does not create a new project.
        ---
        consumes:
        - application/json
        parameters:
        - in: path
          name: project_id
          type: string
          required: True
          description: Unique project id
        - in: body
          name: action
          description: Actions want to apply to the video
          required: True
          schema:
            type: object
            properties:
              cut:
                type: object
                properties:
                  start:
                    type: integer
                    example: 5
                  end:
                    type: integer
                    example: 10
              crop:
                type: object
                properties:
                  width:
                    type: integer
                    example: 480
                  height:
                    type: integer
                    example: 360
                  x:
                    type: integer
                    example: 10
                  y:
                    type: integer
                    example: 10
              rotate:
                type: object
                properties:
                  degree:
                    type: integer
                    example: 90
        responses:
          200:
            description: OK
            schema:
              type: object
              properties:
                filename:
                  type: string
                  example: fa5079a38e0a4197864aa2ccb07f3bea.mp4
                url:
                  type: string
                  example: https://example.com/raw/fa5079a38e0a4197864aa2ccb07f3bea
                storage_id:
                  type: string
                  example: 2019/5/fa5079a38e0a4197864aa2ccb07f3bea.mp4
                metadata:
                  type: object
                  properties:
                    codec_name:
                      type: string
                      example: h264
                    codec_long_name:
                      type: string
                      example: H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
                    width:
                      type: integer
                      example: 640
                    height:
                      type: integer
                      example: 360
                    duration:
                      type: float
                      example: 300.014000
                    bit_rate:
                      type: int
                      example: 287654
                    nb_frames:
                      type: int
                      example: 7654
                    r_frame_rate:
                      type: string
                      example: 24/1
                    format_name:
                      type: string
                      example: mov,mp4,m4a,3gp,3g2,mj2
                    size:
                      type: int
                      example: 14567890
                mime_type:
                  type: string
                  example: video/mp4
                create_time:
                  type: string
                  example: 2019-05-01T09:00:00+00:00
                original_filename:
                  type: string
                  example: video.mp4
                request_address:
                  type: string
                  example: 127.0.0.1
                version:
                  type: integer
                  example: 1
                parent:
                  type: object
                  example: {}
                processing:
                  type: boolean
                  example: False
                thumbnails:
                  type: object
                  example: {}
                _id:
                  type: string
                  example: 5cbd5acfe24f6045607e51aa
        """

        if self._project['processing']['video']:
            return json_response({'processing': True}, status=202)

        # TODO do we really need this restriction?
        # if self._project.get('version') == 1:
        #     raise BadRequest("Project with version 1 can't be edited. Use POST instead.")

        request_json = request.get_json()
        document = validate_document(
            request_json if request_json else {},
            self.schema_edit
        )
        metadata = self._project['metadata']

        # validate trim
        if 'trim' in document:
            if document['trim']['start'] >= document['trim']['end']:
                raise BadRequest({"trim": [{"start": ["must be less than 'end' value"]}]})
            elif (document['trim']['end'] - document['trim']['start'] <= app.config.get('MIN_TRIM_DURATION')) \
                    or (metadata['duration'] - document['trim']['start'] < app.config.get('MIN_TRIM_DURATION')):
                raise BadRequest({"trim": [
                    {"start": [f"trimmed video must be at least {app.config.get('MIN_TRIM_DURATION')} seconds"]}
                ]})
            elif document['trim']['end'] > metadata['duration']:
                raise BadRequest({"trim": [
                    {"end": [f"outside of initial video's length"]}
                ]})
            elif document['trim']['start'] == 0 and document['trim']['end'] == metadata['duration']:
                raise BadRequest({"trim": [
                    {"end": ["trim is duplicating an entire video"]}
                ]})
        # validate crop
        if 'crop' in document:
            if metadata['width'] - document['crop']['x'] < app.config.get('MIN_VIDEO_WIDTH'):
                raise BadRequest({"crop": [{"x": ["less than minimum allowed crop width"]}]})
            elif metadata['height'] - document['crop']['y'] < app.config.get('MIN_VIDEO_HEIGHT'):
                raise BadRequest({"crop": [{"y": ["less than minimum allowed crop height"]}]})
            elif document['crop']['x'] + document['crop']['width'] > metadata['width']:
                raise BadRequest({"crop": [{"width": ["crop's frame is outside a video's frame"]}]})
            elif document['crop']['y'] + document['crop']['height'] > metadata['height']:
                raise BadRequest({"crop": [{"height": ["crop's frame is outside a video's frame"]}]})
        # validate scale
        if 'scale' in document:
            width = metadata['width']
            if 'crop' in document:
                width = document['crop']['width']
            if document['scale'] == width:
                raise BadRequest({"trim": [
                    {"scale": ["video or crop option already has exactly the same width"]}
                ]})
            elif not app.config.get('ALLOW_INTERPOLATION') and document['scale'] > width:
                raise BadRequest({"trim": [
                    {"scale": ["interpolation of pixels is not allowed"]}
                ]})
            elif app.config.get('ALLOW_INTERPOLATION') \
                    and document['scale'] > width \
                    and width >= app.config.get('INTERPOLATION_LIMIT'):
                raise BadRequest({"trim": [
                    {"scale": [f"interpolation is permitted only for videos which have width less than "
                               f"{app.config.get('INTERPOLATION_LIMIT')}px"]}
                ]})

        # set processing flag
        self._project = app.mongo.db.projects.find_one_and_update(
            {'_id': self._project['_id']},
            {'$set': {'processing.video': True}},
            return_document=ReturnDocument.AFTER
        )
        save_activity_log("PUT PROJECT", self._project['_id'], document)

        # run task
        edit_video.delay(
            json_util.dumps(self._project),
            changes=document
        )

        return json_response({"processing": True}, status=200)

    def delete(self, project_id):
        """
        Delete project from db and video from filestorage.
        ---
        parameters:
        - name: project_id
          in: path
          type: string
          required: true
          description: Unique project id
        responses:
          204:
            description: NO CONTENT
        """

        # remove project dir from storage
        app.fs.delete_dir(self._project['storage_id'])
        save_activity_log("DELETE PROJECT", self._project['_id'])
        app.mongo.db.projects.delete_one({'_id': self._project['_id']})

        return json_response(status=204)


class DuplicateProject(MethodView):

    def post(self, project_id):
        if any(self._project['processing'].values()):
            return json_response({"processing": True}, status=202)

        # deepcopy & save a child_project
        child_project = copy.deepcopy(self._project)
        del child_project['_id']
        del child_project['storage_id']
        child_project['parent'] = self._project['_id']
        child_project['create_time'] = datetime.utcnow()
        child_project['version'] += 1
        child_project['thumbnails'] = {
            'timeline': [],
            'preview': None
        }
        app.mongo.db.projects.insert_one(child_project)

        # put a video file stream into storage
        try:
            storage_id = app.fs.put(
                content=app.fs.get(self._project['storage_id']),
                filename=child_project['filename'],
                project_id=child_project['_id'],
                content_type=child_project['mime_type']
            )
        except Exception as e:
            # remove record from db
            app.mongo.db.projects.delete_one({'_id': child_project['_id']})
            raise InternalServerError(str(e))

        try:
            # set 'storage_id' for child_project
            child_project = app.mongo.db.projects.find_one_and_update(
                {'_id': child_project['_id']},
                {'$set': {'storage_id': storage_id}},
                return_document=ReturnDocument.AFTER
            )

            # save preview thumbnail
            if self._project['thumbnails']['preview']:
                storage_id = app.fs.put(
                    content=app.fs.get(self._project['thumbnails']['preview']['storage_id']),
                    filename=self._project['thumbnails']['preview']['filename'],
                    project_id=None,
                    asset_type='thumbnails',
                    storage_id=child_project['storage_id'],
                    content_type=self._project['thumbnails']['preview']['mimetype']
                )
                child_project['thumbnails']['preview'] = self._project['thumbnails']['preview']
                child_project['thumbnails']['preview']['storage_id'] = storage_id
                # set preview thumbnail in db
                child_project = app.mongo.db.projects.find_one_and_update(
                    {'_id': child_project['_id']},
                    {"$set": {
                        'thumbnails.preview': child_project['thumbnails']['preview']
                    }},
                    return_document=ReturnDocument.AFTER
                )

            # save timeline thumbnails
            timeline_thumbnails = []
            for thumbnail in self._project['thumbnails']['timeline']:
                storage_id = app.fs.put(
                    content=app.fs.get(thumbnail['storage_id']),
                    filename=thumbnail['filename'],
                    project_id=None,
                    asset_type='thumbnails',
                    storage_id=child_project['storage_id'],
                    content_type=thumbnail['mimetype']
                )
                timeline_thumbnails.append({
                    'filename': thumbnail['filename'],
                    'storage_id': storage_id,
                    'mimetype': thumbnail['mimetype'],
                    'width': thumbnail['width'],
                    'height': thumbnail['height'],
                    'size': thumbnail['size']
                })
            if timeline_thumbnails:
                child_project = app.mongo.db.projects.find_one_and_update(
                    {'_id': child_project['_id']},
                    {"$set": {
                        'thumbnails.timeline': timeline_thumbnails
                    }},
                    return_document=ReturnDocument.AFTER
                )

        except Exception as e:
            # delete child_project dir
            app.fs.delete_dir(storage_id)
            # remove record from db
            app.mongo.db.projects.delete_one({'_id': child_project['_id']})
            raise InternalServerError(str(e))

        save_activity_log('duplicated', self._project['_id'])
        add_urls(child_project)

        return json_response(child_project, status=201)


class RetrieveOrCreateThumbnails(MethodView):
    SCHEMA_THUMBNAILS = {
        'type': {
            'type': 'string',
            'required': True,
            'anyof': [
                {
                    'allowed': ['timeline'],
                    'dependencies': ['amount'],
                    'excludes': 'position',
                },
                {
                    # make `amount` optional
                    'allowed': ['timeline'],
                    'excludes': 'position',
                },
                {
                    'allowed': ['preview'],
                    'dependencies': ['position'],
                    'excludes': 'amount',
                }
            ],
        },
        'amount': {
            'type': 'integer',
            'coerce': int,
            'min': 1,
        },
        'position': {
            'type': 'float',
            'coerce': float,
        },
    }

    SCHEMA_UPLOAD = {
        'file': {
            'type': 'filestorage',
            'required': True
        }
    }

    def get(self, project_id):
        """
        Get or capture video thumbnails.
        Generate new thumbnails if it is empty or `amount` argument different from current total thumbnails.
        Or capture preview thumbnail at `position`.
        ---
        consumes:
        - application/json
        parameters:
        - in: path
          name: project_id
          type: string
          required: True
          description: Unique project id
        - name: amount
          in: query
          type: integer
          description: number thumbnails to create
        - name: position
          in: query
          type: float
          description: position to capture preview thumbnail
        responses:
          200:
            description: OK
            schema:
              type: object
              properties:
                processing:
                  type: boolean
                  example: True
                thumbnails:
                  type: object
                  example: {}
        """
        document = validate_document(request.args.to_dict(), self.SCHEMA_THUMBNAILS)
        add_urls(self._project)

        if document['type'] == 'timeline':
            return self._get_timeline_thumbnails(
                amount=document.get('amount', app.config.get('DEFAULT_TOTAL_TIMELINE_THUMBNAILS'))
            )

        return self._get_preview_thumbnail(document['position'])

    def post(self, project_id):
        """
        Update video preview thumbnail
        ---
        consumes:
        - application/json
        parameters:
        - in: path
          name: project_id
          type: string
          required: True
          description: Unique project id
        - in: body
          name: body
          description: Thumbnail data
          required: True
          schema:
            type: object
            properties:
              data:
                type: string
                description: base64 image data want to upload
        responses:
          200:
            description: OK
            schema:
              type: object
              properties:
                filename:
                  type: string
                  example: fa5079a38e0a4197864aa2ccb07f3bea.mp4
                url:
                  type: string
                  example: https://example.com/raw/fa5079a38e0a4197864aa2ccb07f3bea
                storage_id:
                  type: string
                  example: 2019/5/fa5079a38e0a4197864aa2ccb07f3bea.mp4
                metadata:
                  type: object
                  properties:
                    codec_name:
                      type: string
                      example: h264
                    codec_long_name:
                      type: string
                      example: H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
                    width:
                      type: string
                      example: 640
                    height:
                      type: string
                      example: 360
                    duration:
                      type: float
                      example: 300.014000
                    bit_rate:
                      type: int
                      example: 287654
                    nb_frames:
                      type: int
                      example: 7654
                    r_frame_rate:
                      type: string
                      example: 24/1
                    format_name:
                      type: string
                      example: mov,mp4,m4a,3gp,3g2,mj2
                    size:
                      type: int
                      example: 14567890
                mime_type:
                  type: string
                  example: video/mp4
                create_time:
                  type: string
                  example: 2019-05-01T09:00:00+00:00
                original_filename:
                  type: string
                  example: video.mp4
                request_address:
                  type: string
                  example: 127.0.0.1
                version:
                  type: integer
                  example: 1
                parent:
                  type: object
                  example: {}
                processing:
                  type: boolean
                  example: False
                thumbnails:
                  type: object
                  example: {}
                _id:
                  type: string
                  example: 5cbd5acfe24f6045607e51aa
                preview_thumbnail:
                  type: object
                  properties:
                    filename:
                      type: string
                      example: fa5079a38e0a4197864aa2ccb07f3bea_thumbnail.png
                    storage_id:
                      type: string
                      example: 2019/5/fa5079a38e0a4197864aa2ccb07f3bea_thumbnail.png
                    mimetype:
                      type: string
                      example: "image/png"
                    width:
                      type: integer
                      example: 640
                    height:
                      type: integer
                      example: 360
                    size:
                      type: int
                      example: 300000
        """

        # validate request
        if 'file' not in request.files:
            # to avoid TypeError: cannot serialize '_io.BufferedRandom' error
            raise BadRequest({"file": ["required field"]})
        document = validate_document(request.files, self.SCHEMA_UPLOAD)

        # validate codec
        file_stream = document['file'].stream.read()
        metadata = get_video_editor().get_meta(file_stream)
        if metadata.get('codec_name') not in app.config.get('CODEC_SUPPORT_IMAGE'):
            raise BadRequest(f"Codec: '{metadata.get('codec_name')}' is not supported.")

        # check if busy
        if self._project['processing']['thumbnail_preview']:
            return json_response({'processing': True}, status=202)

        # save to fs
        thumbnail_filename = "{filename}_preview-custom.{original_ext}".format(
            filename=os.path.splitext(self._project['filename'])[0],
            original_ext=request.files['file'].filename.rsplit('.', 1)[-1].lower()
        )
        mimetype = app.config.get('CODEC_MIMETYPE_MAP')[metadata.get('codec_name')]
        storage_id = app.fs.put(
            content=file_stream,
            filename=thumbnail_filename,
            project_id=None,
            asset_type='thumbnails',
            storage_id=self._project['storage_id'],
            content_type=mimetype
        )

        # delete old file
        if self._project['thumbnails']['preview'] \
                and storage_id != self._project['thumbnails']['preview']['storage_id']:
            app.fs.delete(self._project['thumbnails']['preview']['storage_id'])

        # save new thumbnail info
        self._project = app.mongo.db.projects.find_one_and_update(
            {'_id': self._project['_id']},
            {'$set': {
                'thumbnails.preview': {
                    'filename': thumbnail_filename,
                    'storage_id': storage_id,
                    'mimetype': mimetype,
                    'width': metadata.get('width'),
                    'height': metadata.get('height'),
                    'size': metadata.get('size'),
                    'position': 'custom'
                }
            }},
            return_document=ReturnDocument.AFTER
        )
        add_urls(self._project)

        return json_response(self._project['thumbnails']['preview'])

    def _get_timeline_thumbnails(self, amount):
        """
        Get list or create thumbnails for timeline
        :param amount: amount of thumbnails
        :return: json response
        """
        # resource is busy
        if self._project['processing']['thumbnails_timeline']:
            return json_response({"processing": True}, status=202)
        # no need to generate thumbnails
        elif amount == len(self._project['thumbnails']['timeline']):
            return json_response(self._project['thumbnails']['timeline'])
        else:
            # set processing flag
            self._project = app.mongo.db.projects.find_one_and_update(
                {'_id': self._project['_id']},
                {'$set': {'processing.thumbnails_timeline': True}},
                return_document=ReturnDocument.AFTER
            )
            # run task
            generate_timeline_thumbnails.delay(
                json_util.dumps(self._project),
                amount
            )
            return json_response({"processing": True}, status=200)

    def _get_preview_thumbnail(self, position):
        """
        Get or create thumbnail for preview
        :param position: video position to capture a frame
        :return: json response
        """
        # resource is busy
        if self._project['processing']['thumbnail_preview']:
            return json_response({"processing": True}, status=202)
        elif (self._project['thumbnails']['preview'] and
              self._project['thumbnails']['preview'].get('position') == position):
            return json_response(self._project['thumbnails']['preview'])
        elif self._project['metadata']['duration'] < position:
            return BadRequest(
                f"Requested position:{position} is more than video's duration:{self._project['metadata']['duration']}."
            )
        else:
            # set processing flag
            self._project = app.mongo.db.projects.find_one_and_update(
                {'_id': self._project['_id']},
                {'$set': {'processing.thumbnail_preview': True}},
                return_document=ReturnDocument.AFTER
            )
            # run task
            generate_preview_thumbnail.delay(
                json_util.dumps(self._project),
                position
            )
            return json_response({"processing": True}, status=200)


class GetRawVideo(MethodView):
    def get(self, project_id):
        """
        Get video
        ---
        parameters:
        - in: path
          name: project_id
          type: string
          required: True
        produces:
          - video/mp4
        responses:
          200:
            description: OK
            schema:
              type: file
        """

        # video is processing
        if self._project['processing']['video']:
            return json_response({'processing': True}, status=202)

        # get stream file for video
        video_range = request.headers.environ.get('HTTP_RANGE')
        length = self._project['metadata'].get('size')
        if video_range:
            start = int(re.split('[= | -]', video_range)[1])
            end = length - 1
            chunksize = end - start + 1
            headers = {
                'Content-Range': f'bytes {start}-{end}/{length}',
                'Accept-Ranges': 'bytes',
                'Content-Length': chunksize,
                'Content-Type': self._project.get("mime_type"),
            }
            # get a stack of bytes push to client
            stream = app.fs.get_range(self._project['storage_id'], start, chunksize)
            res = make_response(stream)
            res.headers = headers
            return res, 206

        headers = {
            'Content-Length': length,
            'Content-Type': 'video/mp4',
        }
        stream = app.fs.get(self._project.get('storage_id'))
        res = make_response(stream)
        res.headers = headers
        return res, 200


class GetRawThumbnail(MethodView):
    SCHEMA_THUMBNAILS = {
        'type': {
            'type': 'string',
            'required': True,
            'anyof': [
                {
                    'allowed': ['timeline'],
                    'dependencies': ['index']
                },
                {
                    'allowed': ['preview'],
                    'excludes': 'index'
                }
            ],
        },
        'index': {
            'type': 'integer',
            'coerce': int,
            'min': 0
        }
    }

    def get(self, project_id):
        """
        Get thumbnail file
        ---
        parameters:
        - in: path
          name: project_id
          type: string
          required: True
          description: Unique project id
        - in: query
          name: type
          type: string
          description: timeline or preview
        - in: query
          name: index
          type: integer
          description: index of timeline thumbnail to read, used only when type is preview
        produces:
          - image/png
        responses:
          200:
            description: OK
            schema:
              type: file
        """

        document = validate_document(request.args.to_dict(), self.SCHEMA_THUMBNAILS)

        # preview
        if document['type'] == 'preview':
            if not self._project['thumbnails']['preview']:
                raise NotFound()
            thumbnail = self._project['thumbnails']['preview']
            byte = app.fs.get(thumbnail['storage_id'])
        # timeline
        else:
            try:
                thumbnail = self._project['thumbnails']['timeline'][document['index']]
            except IndexError:
                raise NotFound()
            byte = app.fs.get(thumbnail['storage_id'])

        res = make_response(byte)
        res.headers['Content-Type'] = thumbnail['mimetype']
        return res


# register all urls
bp.add_url_rule(
    '/',
    view_func=ListUploadProject.as_view('upload_project')
)
bp.add_url_rule(
    '/<project_id>',
    view_func=RetrieveEditDestroyProject.as_view('retrieve_edit_destroy_project')
)
bp.add_url_rule(
    '/<project_id>/duplicate',
    view_func=DuplicateProject.as_view('duplicate_project')
)
bp.add_url_rule(
    '/<project_id>/thumbnails',
    view_func=RetrieveOrCreateThumbnails.as_view('retrieve_or_create_thumbnails')
)
bp.add_url_rule(
    '/<project_id>/raw/video',
    view_func=GetRawVideo.as_view('get_raw_video')
)
bp.add_url_rule(
    '/<project_id>/raw/thumbnail',
    view_func=GetRawThumbnail.as_view('get_raw_thumbnail')
)
