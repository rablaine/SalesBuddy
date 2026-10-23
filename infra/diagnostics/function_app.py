"""Azure Python Functions v2 entry points, behind APIM and mandatory EasyAuth."""

import json
import logging
import os
from functools import lru_cache

import azure.functions as func

from service import (
    Conflict, InvalidContact, InvalidPayload, RateLimited, authorized, configured_service,
    parse_payload, trusted_contact, validate_rate_key,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def service():
    """Reuse managed-identity clients in the Functions worker."""
    return configured_service()


def receive(req: func.HttpRequest, followup: bool) -> func.HttpResponse:
    """Validate trust and content before creating a durable receipt."""
    if not authorized(req.headers, os.environ.get('APIM_PRINCIPAL_ID', '')):
        return func.HttpResponse(status_code=403)
    try:
        rate_key = validate_rate_key(req.headers.get('X-SalesBuddy-Rate-Key', ''))
        payload = parse_payload(req.get_body(), followup)
        contact = trusted_contact(req.headers.get('X-SalesBuddy-Contact', '')) if followup else None
        service().accept(payload, contact, rate_key=rate_key)
    except RateLimited as error:
        return func.HttpResponse(
            json.dumps({'error': 'submission_limit_reached'}),
            status_code=429, mimetype='application/json',
            headers={'Retry-After': str(error.retry_after)})
    except InvalidContact:
        return func.HttpResponse(
            json.dumps({'error': 'contact_email_unavailable',
                        'message': 'No usable email contact claim is available.'}),
            status_code=422, mimetype='application/json')
    except InvalidPayload:
        return func.HttpResponse('Invalid diagnostics payload', status_code=400)
    except Conflict:
        return func.HttpResponse('Report identifier conflict', status_code=409)
    except Exception:
        logger.error('Diagnostics durable receipt unavailable')
        return func.HttpResponse('Temporarily unavailable', status_code=503)
    return func.HttpResponse(
        json.dumps({'report_id': payload['report_id'], 'status': 'accepted'}),
        status_code=202, mimetype='application/json')


@app.route(route='reports', methods=['POST'])
def reports(req: func.HttpRequest) -> func.HttpResponse:
    """Accept anonymous sanitized failure reports, ignoring all identity headers."""
    return receive(req, False)


@app.route(route='followups', methods=['POST'])
def followups(req: func.HttpRequest) -> func.HttpResponse:
    """Accept a separate explicit contact opt-in from a validated APIM email claim."""
    return receive(req, True)


@app.route(route='health', methods=['GET'])
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Return no infrastructure or configuration information."""
    if not authorized(req.headers, os.environ.get('APIM_PRINCIPAL_ID', '')):
        return func.HttpResponse(status_code=403)
    return func.HttpResponse('ok', status_code=200)


@app.queue_trigger(arg_name='message', queue_name='%DIAGNOSTICS_QUEUE_NAME%',
                   connection='REPORT_QUEUE')
def dispatch(message: func.QueueMessage) -> None:
    """Wake a durable event immediately after its receipt."""
    service().process(message.get_body().decode('utf-8'))


@app.timer_trigger(schedule='0 * * * * *', arg_name='timer', run_on_startup=False,
                   use_monitor=True)
def repair(timer: func.TimerRequest) -> None:
    """Repair lost enqueue attempts, poll ACS and expire reports and contacts."""
    service().repair()
