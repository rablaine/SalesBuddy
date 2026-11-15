"""Independent timer consumer of the email application's existing success records."""

import json
import logging
import os
import re
from functools import lru_cache

import azure.functions as func

from service import (
    configured_service, configured_token_upload, parse_lab_token, prepare_token_upload, read_json,
    store_lab_token, upload_target, upload_window_open,
)

app = func.FunctionApp()
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def service():
    """Reuse managed-identity clients within this separate Function worker."""
    return configured_service()


@app.timer_trigger(schedule='0 * * * * *', arg_name='timer', run_on_startup=False,
                   use_monitor=True)
def investigate(timer: func.TimerRequest) -> None:
    """Remain inert unless explicitly enabled; failures never reach the email application."""
    setting = os.environ.get('INVESTIGATION_ENABLED', 'false')
    if setting == 'false':
        return
    if setting != 'true':
        raise ValueError('INVESTIGATION_ENABLED must be true or false')
    try:
        service().tick()
    except Exception:
        logger.error('Isolated investigation tick failed; existing email delivery is unaffected')
        raise


@app.route(route='status', methods=['GET'], auth_level=func.AuthLevel.FUNCTION)
def status(req: func.HttpRequest) -> func.HttpResponse:
    """Expose experiment status to an operator with a Function key, never packet contents."""
    setting = os.environ.get('INVESTIGATION_ENABLED', 'false')
    if setting == 'false':
        return func.HttpResponse(json.dumps({'enabled': False}), mimetype='application/json')
    if setting != 'true':
        raise ValueError('INVESTIGATION_ENABLED must be true or false')
    from azure.core.exceptions import ResourceNotFoundError

    worker = service()
    if worker.profile == 'production':
        return func.HttpResponse(json.dumps(worker.status_snapshot()), mimetype='application/json')
    try:
        record = read_json(worker.state.get_blob_client(
            f'investigations/{worker.report_id}.json'))
    except ResourceNotFoundError:
        record = {'status': 'waiting_for_email'}
    result = {key: record[key] for key in
              ('report_id', 'status', 'task_id', 'task_url', 'http_status') if key in record}
    for field in ('notification', 'email_preview'):
        if record.get(field):
            result[field] = {key: record[field][key] for key in
                             ('status', 'stage', 'http_status', 'pr_url', 'delivery_kind')
                             if key in record[field]}
    try:
        upload = read_json(worker.state.get_blob_client('token-upload.json'))
    except ResourceNotFoundError:
        pass
    else:
        result['token_upload_status'] = upload.get('status')
        if type(upload.get('http_status')) is int:
            result['token_upload_http_status'] = upload['http_status']
    return func.HttpResponse(json.dumps(result), mimetype='application/json')


@app.route(route='token-upload', methods=['GET', 'POST'], auth_level=func.AuthLevel.FUNCTION)
def upload_token(req: func.HttpRequest) -> func.HttpResponse:
    """Temporarily accept a lab-only token under Function-key authentication and fixed expiry."""
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

    headers = {'Cache-Control': 'no-store'}
    try:
        opened = upload_window_open(os.environ.get('TOKEN_UPLOAD_EXPIRES_AT', ''))
    except ValueError:
        logger.error('Lab token upload expiry configuration is invalid')
        return func.HttpResponse(status_code=503, headers=headers)
    if not opened:
        return func.HttpResponse(status_code=410, headers=headers)
    if not req.url.startswith('https://'):
        return func.HttpResponse(status_code=415, headers=headers)
    try:
        profile = os.environ.get('TOKEN_UPLOAD_PROFILE', 'synthetic')
        secret_name, _, repository = upload_target(profile)
    except ValueError:
        logger.error('Token upload profile configuration is invalid')
        return func.HttpResponse(status_code=503, headers=headers)
    if req.method == 'GET':
        try:
            state, vault = configured_token_upload()
            try:
                vault.get_secret(secret_name)
                present = True
            except ResourceNotFoundError:
                present = False
            marker = read_json(prepare_token_upload(state, profile))
            upload_status = marker['status']
        except HttpResponseError as error:
            logger.error('Lab upload preparation failed with Azure HTTP %s', error.status_code)
            return func.HttpResponse(json.dumps({
                'error': 'preparation_failed', 'azure_http_status': error.status_code,
            }), status_code=503, mimetype='application/json', headers=headers)
        except Exception:
            logger.error('Lab vault readiness check unavailable')
            return func.HttpResponse(json.dumps({'error': 'preparation_failed'}),
                                     status_code=503, mimetype='application/json', headers=headers)
        return func.HttpResponse(json.dumps({
            'status': 'ready', 'secret_present': present, 'upload_status': upload_status,
            'profile': profile, 'repository': repository,
        }),
                                 mimetype='application/json', headers=headers)
    if (
            req.headers.get('Content-Type', '').split(';')[0].strip().lower() !=
            'application/json'):
        return func.HttpResponse(status_code=415, headers=headers)
    try:
        if req.headers.get('X-Upload-Profile', 'synthetic') != profile:
            raise ValueError('Invalid token upload profile')
        token = parse_lab_token(req.get_body())
        request_id = req.headers.get('X-Lab-Upload-Id', '')
        if re.fullmatch(r'[0-9a-f]{32}', request_id) is None:
            raise ValueError('Invalid token upload request ID')
    except ValueError:
        logger.warning('Lab token upload rejected invalid input')
        return func.HttpResponse('Invalid token upload', status_code=400, headers=headers)
    try:
        state, vault = configured_token_upload()
        outcome = store_lab_token(token, state, vault, request_id, profile)
        if outcome != 'uploaded':
            logger.warning('Lab token upload not completed: %s', outcome)
            return func.HttpResponse(json.dumps({'error': outcome}),
                                     status_code=503 if outcome in {'busy', 'permission_pending'}
                                     else 409, mimetype='application/json', headers=headers)
    except HttpResponseError as error:
        logger.error('Lab token upload failed with Azure HTTP %s', error.status_code)
        return func.HttpResponse(json.dumps({'error': 'upload_failed',
                                            'azure_http_status': error.status_code}),
                                 status_code=503, mimetype='application/json', headers=headers)
    except Exception:
        logger.error('Lab token upload failed; no credential content is logged')
        return func.HttpResponse(json.dumps({'error': 'upload_failed'}),
                                 status_code=503, mimetype='application/json', headers=headers)
    finally:
        token = ''
    logger.info('Lab token uploaded; Copilot submission remains independently gated')
    return func.HttpResponse(json.dumps({'status': 'uploaded'}), status_code=201,
                             mimetype='application/json', headers=headers)
