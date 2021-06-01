import asyncio
from random import uniform
from ..errorHandler import TooManyRequestsException
from ...metaApi.models import date, format_error
from datetime import datetime
from typing import List


class SubscriptionManager:
    """Subscription manager to handle account subscription logic."""

    def __init__(self, websocket_client):
        """Inits the subscription manager.

        Args:
            websocket_client: Websocket client to use for sending requests.
        """
        self._websocketClient = websocket_client
        self._subscriptions = {}
        self._awaitingResubscribe = {}

    def is_account_subscribing(self, account_id: str):
        """Returns whether an account is currently subscribing."""
        for key in self._subscriptions.keys():
            if key.startswith(account_id):
                return True
        return False

    async def subscribe(self, account_id: str, instance_number: int = None):
        """Schedules to send subscribe requests to an account until cancelled.

        Args:
            account_id: Id of the MetaTrader account.
            instance_number: Instance index number.
        """
        instance_id = account_id + ':' + str(instance_number or 0)
        if instance_id not in self._subscriptions:
            self._subscriptions[instance_id] = {
                'shouldRetry': True,
                'task': None,
                'wait_task': None,
                'future': None
            }
            subscribe_retry_interval_in_seconds = 3
            while self._subscriptions[instance_id]['shouldRetry']:
                async def subscribe_task():
                    try:
                        await self._websocketClient.subscribe(account_id, instance_number)
                    except TooManyRequestsException as err:
                        socket_instance_index = self._websocketClient.socket_instances_by_accounts[account_id]
                        if err.metadata['type'] == 'LIMIT_ACCOUNT_SUBSCRIPTIONS_PER_USER':
                            print(format_error(err))
                        if err.metadata['type'] in ['LIMIT_ACCOUNT_SUBSCRIPTIONS_PER_USER',
                                                    'LIMIT_ACCOUNT_SUBSCRIPTIONS_PER_SERVER',
                                                    'LIMIT_ACCOUNT_SUBSCRIPTIONS_PER_USER_PER_SERVER']:
                            del self._websocketClient.socket_instances_by_accounts[account_id]
                            asyncio.create_task(self._websocketClient.lock_socket_instance(socket_instance_index,
                                                                                           err.metadata))
                        else:
                            nonlocal subscribe_retry_interval_in_seconds
                            retry_time = date(err.metadata['recommendedRetryTime']).timestamp()
                            if datetime.now().timestamp() + subscribe_retry_interval_in_seconds < retry_time:
                                await asyncio.sleep(retry_time - datetime.now().timestamp() -
                                                    subscribe_retry_interval_in_seconds)
                    except Exception as err:
                        pass

                self._subscriptions[instance_id]['task'] = asyncio.create_task(subscribe_task())
                await asyncio.wait({self._subscriptions[instance_id]['task']})
                if not self._subscriptions[instance_id]['shouldRetry']:
                    break
                retry_interval = subscribe_retry_interval_in_seconds
                subscribe_retry_interval_in_seconds = min(subscribe_retry_interval_in_seconds * 2, 300)
                subscribe_future = asyncio.Future()

                async def subscribe_task():
                    await asyncio.sleep(retry_interval)
                    subscribe_future.set_result(True)

                self._subscriptions[instance_id]['wait_task'] = asyncio.create_task(subscribe_task())
                self._subscriptions[instance_id]['future'] = subscribe_future
                result = await self._subscriptions[instance_id]['future']
                self._subscriptions[instance_id]['future'] = None
                if not result:
                    break
            del self._subscriptions[instance_id]

    def cancel_subscribe(self, instance_id: str):
        """Cancels active subscription tasks for an instance id.

        Args:
            instance_id: Instance id to cancel subscription task for.
        """
        if instance_id in self._subscriptions:
            subscription = self._subscriptions[instance_id]
            if subscription['future'] and not subscription['future'].done():
                subscription['future'].set_result(False)
                subscription['wait_task'].cancel()
            if subscription['task']:
                subscription['task'].cancel()
            subscription['shouldRetry'] = False

    def cancel_account(self, account_id):
        """Cancels active subscription tasks for an account.

        Args:
            account_id: Account id to cancel subscription tasks for.
        """
        for instance_id in list(filter(lambda key: key.startswith(account_id), self._subscriptions.keys())):
            self.cancel_subscribe(instance_id)

    def on_timeout(self, account_id: str, instance_number: int = None):
        """Invoked on account timeout.

        Args:
            account_id: Id of the MetaTrader account.
            instance_number: Instance index number.
        """
        if account_id in self._websocketClient.socket_instances_by_accounts and \
                self._websocketClient.connected(self._websocketClient.socket_instances_by_accounts[account_id]):
            asyncio.create_task(self.subscribe(account_id, instance_number))

    async def on_disconnected(self, account_id: str, instance_number: int = None):
        """Invoked when connection to MetaTrader terminal terminated.

        Args:
            account_id: Id of the MetaTrader account.
            instance_number: Instance index number.
        """
        await asyncio.sleep(uniform(1, 5))
        if account_id in self._websocketClient.socket_instances_by_accounts:
            asyncio.create_task(self.subscribe(account_id, instance_number))

    def on_reconnected(self, socket_instance_index: int, reconnect_account_ids: List[str]):
        """Invoked when connection to MetaApi websocket API restored after a disconnect.

        Args:
            socket_instance_index: Socket instance index.
            reconnect_account_ids: Account ids to reconnect.
        """

        async def wait_resubscribe(account_id):
            try:
                if account_id not in self._awaitingResubscribe:
                    self._awaitingResubscribe[account_id] = True
                    while self.is_account_subscribing(account_id):
                        await asyncio.sleep(1)
                    if account_id in self._awaitingResubscribe:
                        del self._awaitingResubscribe[account_id]
                    asyncio.create_task(self.subscribe(account_id))
            except Exception as err:
                print(f'[{datetime.now().isoformat()}] Account {account_id} resubscribe task failed',
                      format_error(err))

        try:
            socket_instances_by_accounts = self._websocketClient.socket_instances_by_accounts
            for instance_id in self._subscriptions.keys():
                account_id = instance_id.split(':')[0]
                if account_id in socket_instances_by_accounts and \
                        socket_instances_by_accounts[account_id] == socket_instance_index:
                    self.cancel_subscribe(instance_id)

            for account_id in reconnect_account_ids:
                asyncio.create_task(wait_resubscribe(account_id))
        except Exception as err:
            print(f'[{datetime.now().isoformat()}] Failed to process subscribe manager reconnected event',
                  format_error(err))
