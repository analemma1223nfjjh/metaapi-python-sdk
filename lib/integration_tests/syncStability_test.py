from .. import MetaApi
from ..metaApi.models import format_date
from socketio import AsyncServer
from aiohttp import web
import pytest
import asyncio
from asyncio import sleep
from mock import patch, AsyncMock, MagicMock
from datetime import datetime, timedelta

sio: AsyncServer = None
client_sid: str = ''
api: MetaApi = None
account_information = {
    'broker': 'True ECN Trading Ltd',
    'currency': 'USD',
    'server': 'ICMarketsSC-Demo',
    'balance': 7319.9,
    'equity': 7306.649913200001,
    'margin': 184.1,
    'freeMargin': 7120.22,
    'leverage': 100,
    'marginLevel': 3967.58283542
}
errors = [
    {
        "id": 1,
        "error": "TooManyRequestsError",
        "message": "One user can connect to one server no more than 300 accounts. Current number of connected "
                   "accounts 300. For more information see https://metaapi.cloud/docs/client/rateLimiting/",
        "metadata": {
            "maxAccountsPerUserPerServer": 300,
            "accountsCount":  300,
            "recommendedRetryTime": format_date(datetime.now() + timedelta(seconds=20)),
            "type": "LIMIT_ACCOUNT_SUBSCRIPTIONS_PER_USER_PER_SERVER"
        }
    },
    {
        "id": 1,
        "error": "TooManyRequestsError",
        "message": "You have used all your account subscriptions quota. You have 50 account subscriptions available "
                   "and have used 50 subscriptions. Please deploy more accounts to get more subscriptions. For more "
                   "information see https://metaapi.cloud/docs/client/rateLimiting/",
        "metadata": {
            "maxAccountsPerUser":  50,
            "accountsCount": 50,
            "recommendedRetryTime": format_date(datetime.now() + timedelta(seconds=20)),
            "type": "LIMIT_ACCOUNT_SUBSCRIPTIONS_PER_USER"
        }
    },
    {
        "id": 1,
        "error": "TooManyRequestsError",
        "message": "You can not subscribe to more accounts on this connection because server is out of capacity. "
                   "Please establish a new connection with a different client-id header value to switch to a "
                   "different server. For more information see https://metaapi.cloud/docs/client/rateLimiting/",
        "metadata": {
            "changeClientIdHeader": True,
            "recommendedRetryTime": format_date(datetime.now() + timedelta(seconds=20)),
            "type": "LIMIT_ACCOUNT_SUBSCRIPTIONS_PER_SERVER"
        }
    }
]


class FakeServer:

    def __init__(self):
        self.app = web.Application()
        self.sio: AsyncServer = None
        self.runner = None
        self.stopped = False
        self.status_task: asyncio.Task = None
        self.client_ids = []

    async def authenticate(self, data):
        await self.sio.emit('synchronization', {'type': 'authenticated', 'accountId': data['accountId'],
                                                'instanceIndex': 0, 'replicas': 1, 'host': 'ps-mpa-0'})

    async def emit_status(self, account_id: str):
        packet = {'connected': True, 'authenticated': True, 'instanceIndex': 0, 'type': 'status',
                  'healthStatus': {'rpcApiHealthy': True}, 'replicas': 1, 'host': 'ps-mpa-0',
                  'connectionId': account_id, 'accountId': account_id}
        await self.sio.emit('synchronization', packet)

    async def create_status_task(self, account_id: str):
        while True:
            await self.emit_status(account_id)
            await sleep(1)

    async def respond_account_information(self, data):
        await self.sio.emit('response', {'type': 'response', 'accountId': data['accountId'],
                                         'requestId': data['requestId'], 'accountInformation': account_information})

    async def sync_account(self, data):
        await self.sio.emit('synchronization', {'type': 'synchronizationStarted', 'accountId': data['accountId'],
                                                'instanceIndex': 0, 'synchronizationId': data['requestId'],
                                                'host': 'ps-mpa-0'})
        await sleep(0.1)
        await self.sio.emit('synchronization', {'type': 'accountInformation', 'accountId': data['accountId'],
                                                'accountInformation': account_information, 'instanceIndex': 0,
                                                'host': 'ps-mpa-0'})
        await self.sio.emit('synchronization', {'type': 'specifications', 'accountId': data['accountId'],
                                                'specifications': [], 'instanceIndex': 0, 'host': 'ps-mpa-0'})
        await self.sio.emit('synchronization', {'type': 'orderSynchronizationFinished',
                                                'accountId': data['accountId'], 'instanceIndex': 0,
                                                'synchronizationId': data['requestId'], 'host': 'ps-mpa-0'})
        await sleep(0.1)
        await self.sio.emit('synchronization', {'type': 'dealSynchronizationFinished',
                                                'accountId': data['accountId'], 'instanceIndex': 0,
                                                'synchronizationId': data['requestId'], 'host': 'ps-mpa-0'})

    async def respond(self, data):
        await self.sio.emit('response', {'type': 'response', 'accountId': data['accountId'],
                                         'requestId': data['requestId']})

    async def emit_error(self, data, error_index, retry_after_seconds):
        error = errors[error_index]
        error['metadata']['recommendedRetryTime'] = format_date(datetime.now() +
                                                                timedelta(seconds=retry_after_seconds))
        await sio.emit('processingError', {**error, 'requestId': data['requestId']})

    def enable_sync(self):
        @self.sio.on('request')
        async def on_request(sid, data):
            if data['type'] == 'subscribe':
                await sleep(0.2)
                await self.respond(data)
                self.status_task = asyncio.create_task(self.create_status_task(data['accountId']))
                await self.authenticate(data)
            elif data['type'] == 'synchronize':
                await self.respond(data)
                await self.sync_account(data)
            elif data['type'] == 'waitSynchronized':
                await self.respond(data)
            elif data['type'] == 'getAccountInformation':
                await self.respond_account_information(data)

    def disable_sync(self):
        @self.sio.on('request')
        async def on_request(sid, data):
            return False

    async def start(self):
        global sio
        sio = AsyncServer(async_mode='aiohttp')
        self.sio = sio

        @sio.event
        async def connect(sid, environ):
            self.client_ids.append(environ['aiohttp.request'].headers['Client-Id'])
            global client_sid
            client_sid = sid
            await sio.emit('response', {'type': 'response'})

        self.enable_sync()
        sio.attach(self.app, socketio_path='ws')
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, 'localhost', 8080)
        await site.start()

    async def stop(self):
        if not self.stopped:
            self.stopped = True
            await self.runner.cleanup()


fake_server: FakeServer = None


@pytest.fixture(autouse=True)
async def run_around_tests():
    global fake_server
    fake_server = FakeServer()
    await fake_server.start()
    global api
    api = MetaApi('token', {'application': 'application', 'domain': 'project-stock.agiliumlabs.cloud',
                            'requestTimeout': 3, 'retryOpts': {'retries': 3, 'minDelayInSeconds': 0.1,
                                                               'maxDelayInSeconds': 0.5,
                                                               'subscribeCooldownInSeconds': 6}})

    async def side_effect_get_account(account_id):
        return {
            '_id': account_id,
            'login': '50194988',
            'name': 'mt5a',
            'server': 'ICMarketsSC-Demo',
            'provisioningProfileId': 'f9ce1f12-e720-4b9a-9477-c2d4cb25f076',
            'magic': 123456,
            'application': 'MetaApi',
            'connectionStatus': 'DISCONNECTED',
            'state': 'DEPLOYED',
            'type': 'cloud',
            'accessToken': '2RUnoH1ldGbnEneCoqRTgI4QO1XOmVzbH5EVoQsA'
        }

    api.metatrader_account_api._metatraderAccountClient.get_account = side_effect_get_account
    api._metaApiWebsocketClient.set_url('http://localhost:8080')
    await api._metaApiWebsocketClient.connect()
    api._metaApiWebsocketClient._resolved = True
    yield
    tasks = [task for task in asyncio.all_tasks() if task is not
             asyncio.tasks.current_task()]
    list(map(lambda task: task.cancel(), tasks))
    await fake_server.stop()


class TestSyncStability:
    @pytest.mark.asyncio
    async def test_sync(self):
        """Should synchronize account"""
        account = await api.metatrader_account_api.get_account('accountId')
        connection = await account.connect()
        await connection.wait_synchronized({'timeoutInSeconds': 10})
        response = await connection.get_account_information()
        assert response == account_information
        assert connection.synchronized and connection.terminal_state.connected and \
               connection.terminal_state.connected_to_broker

    @pytest.mark.asyncio
    async def test_socket_disconnect(self):
        """Should reconnect on server socket crash."""
        with patch('lib.clients.metaApi.metaApiWebsocket_client.asyncio.sleep', new=lambda x: sleep(x / 50)):
            account = await api.metatrader_account_api.get_account('accountId')
            connection = await account.connect()
            await connection.wait_synchronized({'timeoutInSeconds': 10})
            await sio.disconnect(client_sid)
            await sleep(0.1)
            response = await connection.get_account_information()
            assert response == account_information
            assert connection.synchronized and connection.terminal_state.connected and \
                   connection.terminal_state.connected_to_broker

    @pytest.mark.asyncio
    async def test_set_disconnected(self):
        """Should set state to disconnected on timeout."""
        with patch('lib.clients.metaApi.metaApiWebsocket_client.asyncio.sleep', new=lambda x: sleep(x / 50)):
            account = await api.metatrader_account_api.get_account('accountId')
            connection = await account.connect()
            await connection.wait_synchronized({'timeoutInSeconds': 10})
            fake_server.status_task.cancel()

            @sio.event
            async def connect(sid, environ):
                return False

            await sio.disconnect(client_sid)
            await sleep(1.2)
            assert not connection.synchronized
            assert not connection.terminal_state.connected
            assert not connection.terminal_state.connected_to_broker

    @pytest.mark.asyncio
    async def test_resubscribe_on_timeout(self):
        """Should resubscribe on timeout."""
        with patch('lib.clients.metaApi.metaApiWebsocket_client.asyncio.sleep', new=lambda x: sleep(x / 50)):
            account = await api.metatrader_account_api.get_account('accountId')
            connection = await account.connect()
            await connection.wait_synchronized({'timeoutInSeconds': 10})
            fake_server.status_task.cancel()
            await sleep(1.5)
            response = await connection.get_account_information()
            assert response == account_information
            assert connection.synchronized and connection.terminal_state.connected and \
                   connection.terminal_state.connected_to_broker

    @pytest.mark.asyncio
    async def test_subscribe_with_late_response(self):
        """Should synchronize if subscribe response arrives after synchronization."""

        @sio.on('request')
        async def on_request(sid, data):
            if data['type'] == 'subscribe':
                await sleep(0.2)
                fake_server.status_task = asyncio.create_task(fake_server.create_status_task(data['accountId']))
                await fake_server.authenticate(data)
                await sleep(0.4)
                await fake_server.respond(data)
            elif data['type'] == 'synchronize':
                await fake_server.respond(data)
                await fake_server.sync_account(data)
            elif data['type'] == 'waitSynchronized':
                await fake_server.respond(data)
            elif data['type'] == 'getAccountInformation':
                await fake_server.respond_account_information(data)

        account = await api.metatrader_account_api.get_account('accountId')
        connection = await account.connect()
        await connection.wait_synchronized({'timeoutInSeconds': 10})
        response = await connection.get_account_information()
        assert response == account_information
        assert connection.synchronized and connection.terminal_state.connected and \
               connection.terminal_state.connected_to_broker

    @pytest.mark.asyncio
    async def test_wait_redeploy(self):
        """Should wait until account is redeployed after disconnect."""
        with patch('lib.clients.metaApi.metaApiWebsocket_client.asyncio.sleep', new=lambda x: sleep(x / 50)):
            account = await api.metatrader_account_api.get_account('accountId')
            connection = await account.connect()
            await connection.wait_synchronized({'timeoutInSeconds': 10})
            fake_server.status_task.cancel()
            await sio.emit('synchronization', {'type': 'disconnected', 'accountId': 'accountId', 'host': 'ps-mpa-0',
                                               'instanceIndex': 0})
            fake_server.disable_sync()
            await sleep(0.4)
            assert not connection.synchronized
            assert not connection.terminal_state.connected
            assert not connection.terminal_state.connected_to_broker
            await sleep(4)
            fake_server.enable_sync()
            await sleep(0.4)
            assert not connection.synchronized
            assert not connection.terminal_state.connected
            assert not connection.terminal_state.connected_to_broker
            await sleep(4)
            assert connection.synchronized and connection.terminal_state.connected and \
                   connection.terminal_state.connected_to_broker

    @pytest.mark.asyncio
    async def test_resubscribe_on_status_packet(self):
        """Should resubscribe immediately after disconnect on status packet."""
        with patch('lib.clients.metaApi.metaApiWebsocket_client.asyncio.sleep', new=lambda x: sleep(x / 50)):
            account = await api.metatrader_account_api.get_account('accountId')
            connection = await account.connect()
            await connection.wait_synchronized({'timeoutInSeconds': 10})
            fake_server.status_task.cancel()
            await sio.emit('synchronization', {'type': 'disconnected', 'accountId': 'accountId', 'host': 'ps-mpa-0',
                                               'instanceIndex': 0})
            fake_server.disable_sync()
            await sleep(0.4)
            assert not connection.synchronized
            assert not connection.terminal_state.connected
            assert not connection.terminal_state.connected_to_broker
            await sleep(4)
            fake_server.enable_sync()
            await fake_server.emit_status('accountId')
            await sleep(0.4)
            assert connection.synchronized and connection.terminal_state.connected and \
                   connection.terminal_state.connected_to_broker

    @pytest.mark.asyncio
    async def test_429_per_user_limit_subscriptions(self):
        """Should limit subscriptions during per user 429 error."""
        subscribed_accounts = {}

        @sio.on('request')
        async def on_request(sid, data):
            nonlocal subscribed_accounts
            if data['type'] == 'subscribe':
                if len(subscribed_accounts.keys()) < 2:
                    subscribed_accounts[data['accountId']] = True
                    await sleep(0.2)
                    await fake_server.respond(data)
                    fake_server.status_task = asyncio.create_task(fake_server.create_status_task(data['accountId']))
                    await fake_server.authenticate(data)
                else:
                    await fake_server.emit_error(data, 1, 2)
            elif data['type'] == 'synchronize':
                await fake_server.respond(data)
                await fake_server.sync_account(data)
            elif data['type'] == 'waitSynchronized':
                await fake_server.respond(data)
            elif data['type'] == 'getAccountInformation':
                await fake_server.respond_account_information(data)
            elif data['type'] == 'unsubscribe':
                del subscribed_accounts[data['accountId']]
                await fake_server.respond(data)

        with patch('lib.clients.metaApi.metaApiWebsocket_client.asyncio.sleep', new=lambda x: sleep(x / 50)):
            account = await api.metatrader_account_api.get_account('accountId')
            connection = await account.connect()
            await connection.wait_synchronized({'timeoutInSeconds': 3})
            account2 = await api.metatrader_account_api.get_account('accountId2')
            connection2 = await account2.connect()
            await connection2.wait_synchronized({'timeoutInSeconds': 3})
            account3 = await api.metatrader_account_api.get_account('accountId3')
            connection3 = await account3.connect()
            try:
                await connection3.wait_synchronized({'timeoutInSeconds': 3})
                raise Exception('TimeoutException expected')
            except Exception as err:
                assert err.__class__.__name__ == 'TimeoutException'
            await connection2.close()
            await sleep(2)
            assert connection3.synchronized

    @pytest.mark.asyncio
    async def test_429_per_user_retry_after_time(self):
        """Should wait for retry time after per user 429 error."""
        request_timestamp = 0
        subscribed_accounts = {}

        @sio.on('request')
        async def on_request(sid, data):
            nonlocal subscribed_accounts
            nonlocal request_timestamp
            if data['type'] == 'subscribe':
                if len(subscribed_accounts.keys()) < 2 or (request_timestamp != 0 and datetime.now().timestamp() - 2 >
                                                           request_timestamp):
                    subscribed_accounts[data['accountId']] = True
                    await sleep(0.2)
                    await fake_server.respond(data)
                    fake_server.status_task = asyncio.create_task(fake_server.create_status_task(data['accountId']))
                    await fake_server.authenticate(data)
                else:
                    request_timestamp = datetime.now().timestamp()
                    await fake_server.emit_error(data, 1, 3)
            elif data['type'] == 'synchronize':
                await fake_server.respond(data)
                await fake_server.sync_account(data)
            elif data['type'] == 'waitSynchronized':
                await fake_server.respond(data)
            elif data['type'] == 'getAccountInformation':
                await fake_server.respond_account_information(data)
            elif data['type'] == 'unsubscribe':
                del subscribed_accounts[data['accountId']]
                await fake_server.respond(data)

        with patch('lib.clients.metaApi.metaApiWebsocket_client.asyncio.sleep', new=lambda x: sleep(x / 50)):
            account = await api.metatrader_account_api.get_account('accountId')
            connection = await account.connect()
            await connection.wait_synchronized({'timeoutInSeconds': 3})
            account2 = await api.metatrader_account_api.get_account('accountId2')
            connection2 = await account2.connect()
            await connection2.wait_synchronized({'timeoutInSeconds': 3})
            account3 = await api.metatrader_account_api.get_account('accountId3')
            connection3 = await account3.connect()
            try:
                await connection3.wait_synchronized({'timeoutInSeconds': 3})
                raise Exception('TimeoutException expected')
            except Exception as err:
                assert err.__class__.__name__ == 'TimeoutException'
            await sleep(2)
            assert not connection3.synchronized
            await sleep(2.5)
            assert connection3.synchronized

    @pytest.mark.asyncio
    async def test_429_per_server_retry_after_time(self):
        """Should wait for retry time after per server 429 error."""
        sid_by_accounts = {}
        request_timestamp = 0

        @sio.on('request')
        async def on_request(sid, data):
            nonlocal request_timestamp
            if data['type'] == 'subscribe':
                if len(list(filter(lambda account_sid: account_sid == sid, sid_by_accounts.values()))) >= 2 and \
                        (request_timestamp == 0 or datetime.now().timestamp() - 2 < request_timestamp):
                    request_timestamp = datetime.now().timestamp()
                    await fake_server.emit_error(data, 2, 2)
                else:
                    sid_by_accounts[data['accountId']] = sid
                    await sleep(0.2)
                    await fake_server.respond(data)
                    fake_server.status_task = asyncio.create_task(fake_server.create_status_task(data['accountId']))
                    await fake_server.authenticate(data)
            elif data['type'] == 'synchronize':
                await fake_server.respond(data)
                await fake_server.sync_account(data)
            elif data['type'] == 'waitSynchronized':
                await fake_server.respond(data)
            elif data['type'] == 'getAccountInformation':
                await fake_server.respond_account_information(data)

        with patch('lib.clients.metaApi.metaApiWebsocket_client.asyncio.sleep', new=lambda x: sleep(x / 50)):
            account = await api.metatrader_account_api.get_account('accountId')
            connection = await account.connect()
            await connection.wait_synchronized({'timeoutInSeconds': 3})
            account2 = await api.metatrader_account_api.get_account('accountId2')
            connection2 = await account2.connect()
            await connection2.wait_synchronized({'timeoutInSeconds': 3})
            account3 = await api.metatrader_account_api.get_account('accountId3')
            connection3 = await account3.connect()
            await connection3.wait_synchronized({'timeoutInSeconds': 3})
            assert sid_by_accounts['accountId'] == sid_by_accounts['accountId2'] != sid_by_accounts['accountId3']
            await sleep(2)
            account4 = await api.metatrader_account_api.get_account('accountId4')
            connection4 = await account4.connect()
            await connection4.wait_synchronized({'timeoutInSeconds': 3})
            assert sid_by_accounts['accountId'] == sid_by_accounts['accountId4']

    @pytest.mark.asyncio
    async def test_429_per_server_reconnect(self):
        """Should reconnect after per server 429 error if connection has no subscribed accounts."""
        sids = []

        @sio.on('request')
        async def on_request(sid, data):
            if data['type'] == 'subscribe':
                sids.append(sid)
                if len(sids) == 1:
                    await fake_server.emit_error(data, 2, 2)
                else:
                    await sleep(0.2)
                    await fake_server.respond(data)
                    fake_server.status_task = asyncio.create_task(fake_server.create_status_task(data['accountId']))
                    await fake_server.authenticate(data)
            elif data['type'] == 'synchronize':
                await fake_server.respond(data)
                await fake_server.sync_account(data)
            elif data['type'] == 'waitSynchronized':
                await fake_server.respond(data)
            elif data['type'] == 'getAccountInformation':
                await fake_server.respond_account_information(data)

        with patch('lib.clients.metaApi.metaApiWebsocket_client.asyncio.sleep', new=lambda x: sleep(x / 50)):
            account = await api.metatrader_account_api.get_account('accountId')
            connection = await account.connect()
            await connection.wait_synchronized({'timeoutInSeconds': 3})
            assert sids[0] != sids[1]

    @pytest.mark.asyncio
    async def test_429_per_server_unsubscribe(self):
        """Should free a subscribe slot on unsubscribe after per server 429 error."""
        sid_by_accounts = {}

        @sio.on('request')
        async def on_request(sid, data):
            if data['type'] == 'subscribe':
                if len(list(filter(lambda account_sid: account_sid == sid, sid_by_accounts.values()))) >= 2:
                    await fake_server.emit_error(data, 2, 200)
                else:
                    sid_by_accounts[data['accountId']] = sid
                    await sleep(0.2)
                    await fake_server.respond(data)
                    fake_server.status_task = asyncio.create_task(fake_server.create_status_task(data['accountId']))
                    await fake_server.authenticate(data)
            elif data['type'] == 'synchronize':
                await fake_server.respond(data)
                await fake_server.sync_account(data)
            elif data['type'] == 'waitSynchronized':
                await fake_server.respond(data)
            elif data['type'] == 'getAccountInformation':
                await fake_server.respond_account_information(data)
            elif data['type'] == 'unsubscribe':
                del sid_by_accounts[data['accountId']]
                await fake_server.respond(data)

        with patch('lib.clients.metaApi.metaApiWebsocket_client.asyncio.sleep', new=lambda x: sleep(x / 50)):
            account = await api.metatrader_account_api.get_account('accountId')
            connection = await account.connect()
            await connection.wait_synchronized({'timeoutInSeconds': 3})
            account2 = await api.metatrader_account_api.get_account('accountId2')
            connection2 = await account2.connect()
            await connection2.wait_synchronized({'timeoutInSeconds': 3})
            account3 = await api.metatrader_account_api.get_account('accountId3')
            connection3 = await account3.connect()
            await connection3.wait_synchronized({'timeoutInSeconds': 3})
            assert sid_by_accounts['accountId'] == sid_by_accounts['accountId2'] != sid_by_accounts['accountId3']
            await connection2.close()
            account4 = await api.metatrader_account_api.get_account('accountId4')
            connection4 = await account4.connect()
            await connection4.wait_synchronized({'timeoutInSeconds': 3})
            assert sid_by_accounts['accountId'] == sid_by_accounts['accountId4']

    @pytest.mark.asyncio
    async def test_429_per_server_per_user_retry_after_time(self):
        """Should wait for retry time after per server per user 429 error."""
        sid_by_accounts = {}
        request_timestamp = 0

        @sio.on('request')
        async def on_request(sid, data):
            nonlocal request_timestamp
            if data['type'] == 'subscribe':
                if len(list(filter(lambda account_sid: account_sid == sid, sid_by_accounts.values()))) >= 2 and \
                        (request_timestamp == 0 or datetime.now().timestamp() - 2 < request_timestamp):
                    request_timestamp = datetime.now().timestamp()
                    await fake_server.emit_error(data, 0, 2)
                else:
                    sid_by_accounts[data['accountId']] = sid
                    await sleep(0.2)
                    await fake_server.respond(data)
                    fake_server.status_task = asyncio.create_task(fake_server.create_status_task(data['accountId']))
                    await fake_server.authenticate(data)
            elif data['type'] == 'synchronize':
                await fake_server.respond(data)
                await fake_server.sync_account(data)
            elif data['type'] == 'waitSynchronized':
                await fake_server.respond(data)
            elif data['type'] == 'getAccountInformation':
                await fake_server.respond_account_information(data)
            elif data['type'] == 'unsubscribe':
                del sid_by_accounts[data['accountId']]
                await fake_server.respond(data)

        with patch('lib.clients.metaApi.metaApiWebsocket_client.asyncio.sleep', new=lambda x: sleep(x / 50)):
            account = await api.metatrader_account_api.get_account('accountId')
            connection = await account.connect()
            await connection.wait_synchronized({'timeoutInSeconds': 3})
            account2 = await api.metatrader_account_api.get_account('accountId2')
            connection2 = await account2.connect()
            await connection2.wait_synchronized({'timeoutInSeconds': 3})
            account3 = await api.metatrader_account_api.get_account('accountId3')
            connection3 = await account3.connect()
            await connection3.wait_synchronized({'timeoutInSeconds': 3})
            assert sid_by_accounts['accountId'] == sid_by_accounts['accountId2'] != sid_by_accounts['accountId3']
            await sleep(2)
            account4 = await api.metatrader_account_api.get_account('accountId4')
            connection4 = await account4.connect()
            await connection4.wait_synchronized({'timeoutInSeconds': 3})
            assert sid_by_accounts['accountId'] != sid_by_accounts['accountId4']
            await connection2.close()
            account5 = await api.metatrader_account_api.get_account('accountId5')
            connection5 = await account5.connect()
            await connection5.wait_synchronized({'timeoutInSeconds': 3})
            assert sid_by_accounts['accountId'] == sid_by_accounts['accountId5']
