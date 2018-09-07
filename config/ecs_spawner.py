import datetime
import hashlib
import hmac
import json
import os
import urllib

from jupyterhub.spawner import (
    Spawner,
)
from tornado import (
    gen,
)
from tornado.concurrent import (
    Future,
)
from tornado.httpclient import (
    AsyncHTTPClient,
    HTTPError,
    HTTPRequest,
)
from traitlets import (
    Bool,
    Dict,
    Int,
    Unicode,
)


class EcsSpawner(Spawner):

    endpoint = Dict(config=True)
    task_arn = Unicode('')
    task_ip = Unicode('')
    task_port = Int(0)

    # We mostly are able to call the AWS API to determine status. However, when we yield the
    # event loop to create the task, if there is a poll before the creation is complete,
    # we must behave as though we are running/starting, but we have no IDs to use with which
    # to check the task.
    calling_run_task = Bool(False)

    num_polls = Int(0)
    max_polls = 600

    def load_state(self, state):
        ''' Misleading name: this "loads" the state onto self, to be used by other methods '''

        super().load_state(state)

        # Called when first created: we might have no state from a previous invocation
        self.task_arn = state.get('task_arn', '')
        self.task_ip = state.get('task_ip', '')
        self.task_port = state.get('task_port', 0)

    def get_state(self):
        ''' Misleading name: the return value of get_state is saved to the database in order
        to be able to restore after the hub went down '''

        state = super().get_state()
        state['task_arn'] = self.task_arn
        state['task_ip'] = self.task_ip
        state['task_port'] = self.task_port

        return state

    async def poll(self):
        # Return values, as dictacted by the Jupyterhub framework:
        # 0                   == not running, or not starting up, i.e. we need to call start
        # None                == running, or not finished starting
        # 1, or anything else == error

        return \
            None if self.calling_run_task else \
            0 if (self.task_arn == '' or self.task_ip == '' or self.task_port == 0) else \
            None if (await _get_task_status_ip_port(self.log, self.endpoint, self.task_arn))[0] in ALLOWED_STATUSES else \
            1

    async def start(self):
        # We sure we can resume during an interrupted startup
        # The sleeps are to work well with the "progress" generator

        self.has_task_arn = Future()
        self.has_task_ip = Future()
        self.has_server_started = Future()
        self.num_polls = 0

        if self.task_arn == '':
            try:
                self.calling_run_task = True
                run_response = await _run_task(self.log, self.endpoint, self.cmd + ['--debug'], self.get_env())
                self.task_arn = run_response['tasks'][0]['taskArn']
                self.log.debug("Set task arn to (%s)", self.task_arn)
            finally:
                self.calling_run_task = False

        self.has_task_arn.set_result(None)
        await gen.sleep(1)

        if self.task_ip == '' or self.task_port == 0:
            while True:
                self.num_polls += 1
                if self.num_polls >= 600:
                    raise Exception('Task %s took too long to become RUNNING'.format(self.task_arn))

                status, ip, port = await _get_task_status_ip_port(self.log, self.endpoint, self.task_arn)
                if status not in ALLOWED_STATUSES:
                    raise Exception('Task %s is %s'.format(self.task_arn, status))

                if status == 'RUNNING':
                    self.task_ip = ip
                    self.task_port = port
                    self.log.debug("Set task ip to (%s)", self.task_ip)
                    self.log.debug("Set task port to (%s)", self.task_port)
                    break

                await gen.sleep(1)

        self.has_task_ip.set_result(None)
        await gen.sleep(1)

        self.has_server_started.set_result(None)
        await gen.sleep(1)

        return (self.task_ip, self.task_port)

    async def stop(self, now=False):
        if self.task_arn == '':
            return

        self.log.debug('Stopping task (%s)...', self.task_arn)
        await _stop_task(self.log, self.endpoint, self.task_arn)
        self.log.debug('Stopped task (%s)... (done)', self.task_arn)

    async def progress(self):
        yield {
            'progress': 10,
            'message': 'Starting server... assigning ID',
        }

        await self.has_task_arn
        yield {
            'progress': 20,
            'message': f'Starting server... assigned ID {self.task_arn}',
        }

        await gen.sleep(1)
        yield {
            'message': f'Starting server... assigning IP address...',
        }

        await self.has_task_ip
        yield {
            'progress': 30,
            'message': f'Starting server... assigned IP address {self.task_ip}',
        }
        
        await gen.sleep(1)
        yield {
            'message': f'Starting server... launching container. May take a few minutes.',
        }

        await self.has_server_started
        yield {
            "progress": 100,
            "message": 'Server has started',
        }
        await gen.sleep(1)

    def clear_state(self):
        super().clear_state()
        self.task_arn = ''
        self.task_ip = ''
        self.task_port = 0


ALLOWED_STATUSES = ('PROVISIONING', 'PENDING', 'RUNNING')


async def _stop_task(logger, endpoint, task_arn):
    return await _make_ecs_request(logger, endpoint, 'StopTask', {
        'cluster': endpoint['cluster_name'],
        'task': task_arn
    })


async def _get_task_status_ip_port(logger, endpoint, task_arn):
    described_tasks = await _describe_tasks(logger, endpoint, [task_arn])
    task = described_tasks['tasks'][0]
    status = task['lastStatus']
    ip_address_attachements = [
        attachment['value']
        for attachment in task['attachments'][0]['details']
        if attachment['name'] == 'privateIPv4Address'
    ]
    ip_address = ip_address_attachements[0] if ip_address_attachements else ''
    port = endpoint['port']
    return status, ip_address, port


async def _describe_tasks(logger, endpoint, task_arns):
    return await _make_ecs_request(logger, endpoint, 'DescribeTasks', {
        'cluster': endpoint['cluster_name'],
        'tasks': task_arns
    })


async def _run_task(logger, endpoint, command_and_args, env):
    return await _make_ecs_request(logger, endpoint, 'RunTask', {
        'cluster': endpoint['cluster_name'],
        'taskDefinition': endpoint['task_definition_arn'],
        'overrides': {
            'containerOverrides': [{
                'command': command_and_args,
                'environment': [
                    {
                        'name': name,
                        'value': value,
                    } for name, value in env.items()
                ],
                'name': 'jupyterhub-singleuser',
            }],
        },
        'count': 1,
        'launchType': 'FARGATE',
        'networkConfiguration': {
            'awsvpcConfiguration': {
                'assignPublicIp': 'ENABLED',
                'securityGroups': ['sg-00062fd201d4e674b'],
                'subnets': ['subnet-fde8d88a'],
            },
        },
    })


async def _make_ecs_request(logger, endpoint, target, dict_data):
    service = 'ecs'
    body = json.dumps(dict_data).encode('utf-8')
    headers = {
        'X-Amz-Target': f'AmazonEC2ContainerServiceV20141113.{target}',
        'Content-Type': 'application/x-amz-json-1.1',
    }
    path = '/'
    auth_headers = _aws_auth_headers(service, endpoint, 'POST', path, {}, headers, body)
    client = AsyncHTTPClient()
    url = f'https://{endpoint["host"]}{path}'
    request = HTTPRequest(url, method='POST', headers={**headers, **auth_headers}, body=body)
    logger.debug('Making request (%s)', body)
    try:
        response = await client.fetch(request)
    except HTTPError as exception:
        logger.exception('HTTPError from ECS (%s)', exception.response.body)
        raise
    logger.debug('Request response (%s)', response.body)
    return json.loads(response.body)


def _aws_auth_headers(service, endpoint, method, path, query, headers, payload):
    algorithm = 'AWS4-HMAC-SHA256'

    now = datetime.datetime.utcnow()
    amzdate = now.strftime('%Y%m%dT%H%M%SZ')
    datestamp = now.strftime('%Y%m%d')
    credential_scope = f'{datestamp}/{endpoint["region"]}/{service}/aws4_request'
    headers_lower = {
        header_key.lower().strip(): header_value.strip()
        for header_key, header_value in headers.items()
    }
    signed_header_keys = sorted([header_key
                                 for header_key in headers_lower.keys()] + ['host', 'x-amz-date'])
    signed_headers = ';'.join([header_key for header_key in signed_header_keys])

    def signature():
        def canonical_request():
            header_values = {
                **headers_lower,
                'host': endpoint['host'],
                'x-amz-date': amzdate,
            }

            canonical_uri = urllib.parse.quote(path, safe='/~')
            query_keys = sorted(query.keys())
            canonical_querystring = '&'.join([
                urllib.parse.quote(key, safe='~') + '=' + urllib.parse.quote(query[key], safe='~')
                for key in query_keys
            ])
            canonical_headers = ''.join([
                header_key + ':' + header_values[header_key] + '\n'
                for header_key in signed_header_keys
            ])
            payload_hash = hashlib.sha256(payload).hexdigest()

            return f'{method}\n{canonical_uri}\n{canonical_querystring}\n' + \
                   f'{canonical_headers}\n{signed_headers}\n{payload_hash}'

        def sign(key, msg):
            return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

        string_to_sign = \
            f'{algorithm}\n{amzdate}\n{credential_scope}\n' + \
            hashlib.sha256(canonical_request().encode('utf-8')).hexdigest()

        date_key = sign(('AWS4' + endpoint['secret_access_key']).encode('utf-8'), datestamp)
        region_key = sign(date_key, endpoint['region'])
        service_key = sign(region_key, service)
        request_key = sign(service_key, 'aws4_request')
        return sign(request_key, string_to_sign).hex()

    return {
        'x-amz-date': amzdate,
        'Authorization': (
            f'{algorithm} Credential={endpoint["access_key_id"]}/{credential_scope}, ' +
            f'SignedHeaders={signed_headers}, Signature=' + signature()
        ),
    }
