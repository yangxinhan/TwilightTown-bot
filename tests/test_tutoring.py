"""Offline regression tests: no Discord login or MariaDB connection is made."""

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from mysql.connector import Error

CONFIG_IDS = (
    'ADMIN_CHANNEL_ID', 'TUTORING_REQUEST_CHANNEL_ID', 'TUTORING_CATEGORY_ID',
    'TUTORING_ARCHIVE_CATEGORY_ID', 'TAG_PREFERENCE_CHANNEL_ID',
    'DAILY_SCHEDULE_CHANNEL_ID', 'DISCONNECTION_NOTIFICATION_CHANNEL_ID',
)
with patch('dotenv.load_dotenv'), patch.dict(os.environ, dict.fromkeys(CONFIG_IDS, '1')):
    import bot as app

from tutoring_store import TutoringStore


def member(user_id, *, tutor=True):
    user = Mock(spec=discord.Member)
    user.id = user_id
    user.name = f'user-{user_id}'
    user.display_name = user.name
    user.mention = f'<@{user_id}>'
    user.roles = [SimpleNamespace(name='鎮民')] if tutor else []
    return user


def http_error():
    return discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Test failure')


class MemoryStore:
    def __init__(self):
        self.request = dict(id=10, applicant_id=20, preferred_tutor_id=None,
                            status='pending', expired=False)
        self.channels = {}
        self.finishes = 0

    def get_request(self, message_id):
        return dict(self.request)

    def finish_request(self, request_id, status, channel=None):
        if self.request['status'] != 'pending' or self.request['expired']:
            return False
        self.request['status'] = status
        self.finishes += 1
        if channel:
            self.channels[channel[0]] = dict(applicant_id=channel[2], tutor_id=channel[4], status='active')
        return True

    def get_channel(self, channel_id):
        record = self.channels.get(channel_id)
        return dict(record) if record else None

    def close_channel(self, channel_id):
        self.channels[channel_id]['status'] = 'closed'
        return True


class TutoringFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.patches = [
            patch.object(app, 'tutoring_store', self.store),
            patch.object(app, 'send_log', AsyncMock()),
            patch.object(app, 'notify_tutoring_user', AsyncMock()),
            patch.object(app, 'tutoring_mention', AsyncMock(side_effect=lambda user: user.mention)),
            patch.object(app, 'logger', Mock()),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.applicant = member(20, tutor=False)
        self.tutor = member(30)
        self.guild = Mock(spec=discord.Guild)
        self.guild.default_role = Mock(spec=discord.Role)
        self.guild.me = member(99)
        self.guild.get_channel.return_value = Mock(spec=discord.CategoryChannel)
        self.guild.get_member.side_effect = lambda user_id: {20: self.applicant, 30: self.tutor}.get(user_id)
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.id = 200
        self.channel.name = '輔導-test'
        self.channel.mention = '<#200>'
        self.channel.overwrites = {}
        self.channel.send = AsyncMock()
        self.channel.delete = AsyncMock()
        self.channel.edit = AsyncMock()
        self.guild.create_text_channel = AsyncMock(return_value=self.channel)

    def interaction(self, *, user=None, message_id=100, channel_id=1):
        return SimpleNamespace(
            user=user or self.tutor, guild=self.guild, channel=self.channel,
            channel_id=channel_id,
            message=SimpleNamespace(id=message_id, embeds=[discord.Embed(title='申請')], edit=AsyncMock()),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_concurrent_accepts_from_fresh_views_create_one_channel(self):
        first, second = self.interaction(), self.interaction(user=member(31))
        await asyncio.gather(app.TutoringRequestView().accept.callback(first),
                             app.TutoringRequestView().accept.callback(second))
        self.guild.create_text_channel.assert_awaited_once()
        self.assertEqual(self.store.finishes, 1)
        self.assertEqual(self.store.request['status'], 'accepted')
        self.assertEqual(self.store.channels[200]['status'], 'active')
        first.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    async def test_accept_and_decline_cannot_both_succeed(self):
        await asyncio.gather(app.TutoringRequestView().accept.callback(self.interaction()),
                             app.TutoringRequestView().decline.callback(self.interaction()))
        self.assertEqual(self.store.finishes, 1)
        self.assertIn(self.store.request['status'], ('accepted', 'declined'))

    async def test_restart_view_uses_persisted_preferred_tutor(self):
        self.store.request['preferred_tutor_id'] = 31
        interaction = self.interaction()
        await app.TutoringRequestView().accept.callback(interaction)
        self.guild.create_text_channel.assert_not_awaited()
        self.assertEqual(self.store.request['status'], 'pending')
        self.assertIn('指定', interaction.followup.send.call_args.args[0])

    async def test_self_accept_and_non_tutor_are_denied(self):
        for user in (member(20), member(31, tutor=False)):
            await app.TutoringRequestView().accept.callback(self.interaction(user=user))
        self.guild.create_text_channel.assert_not_awaited()

    async def test_expired_request_is_denied_before_background_check(self):
        self.store.request['expired'] = True
        await app.TutoringRequestView().accept.callback(self.interaction())
        self.guild.create_text_channel.assert_not_awaited()

    async def test_create_channel_failure_leaves_request_pending(self):
        self.guild.create_text_channel.side_effect = http_error()
        await app.TutoringRequestView().accept.callback(self.interaction())
        self.assertEqual(self.store.request['status'], 'pending')
        self.assertFalse(self.store.channels)

    async def test_welcome_failure_removes_unrecorded_channel(self):
        self.channel.send.side_effect = http_error()
        await app.TutoringRequestView().accept.callback(self.interaction())
        self.channel.delete.assert_awaited_once()
        self.assertEqual(self.store.request['status'], 'pending')

    async def test_database_failure_removes_unrecorded_channel(self):
        with patch.object(self.store, 'finish_request', side_effect=Error('insert failed')):
            await app.TutoringRequestView().accept.callback(self.interaction())
        self.channel.delete.assert_awaited_once()
        self.assertEqual(self.store.request['status'], 'pending')

    async def test_lost_commit_response_preserves_committed_channel(self):
        finish = self.store.finish_request

        def commit_then_disconnect(*args):
            finish(*args)
            raise Error('commit response lost')

        with patch.object(self.store, 'finish_request', side_effect=commit_then_disconnect):
            await app.TutoringRequestView().accept.callback(self.interaction())
        self.channel.delete.assert_not_awaited()
        self.assertEqual(self.store.request['status'], 'accepted')
        app.notify_tutoring_user.assert_awaited_once()

    async def test_unknown_commit_outcome_keeps_channel_for_reconciliation(self):
        interaction = self.interaction()
        with patch.object(self.store, 'finish_request', side_effect=Error('commit lost')), \
                patch.object(self.store, 'get_channel', side_effect=Error('DB offline')):
            await app.TutoringRequestView().accept.callback(interaction)
        self.channel.delete.assert_not_awaited()
        self.assertIn('核對', interaction.followup.send.call_args.args[0])
        app.notify_tutoring_user.assert_not_awaited()

    async def test_request_expiring_during_channel_creation_is_not_accepted(self):
        async def create(*args, **kwargs):
            self.store.request['expired'] = True
            return self.channel

        self.guild.create_text_channel.side_effect = create
        await app.TutoringRequestView().accept.callback(self.interaction())
        self.channel.delete.assert_awaited_once()
        self.assertEqual(self.store.request['status'], 'pending')

    async def test_message_edit_failure_does_not_undo_acceptance(self):
        interaction = self.interaction()
        interaction.message.edit.side_effect = http_error()
        await app.TutoringRequestView().accept.callback(interaction)
        self.assertEqual(self.store.request['status'], 'accepted')
        self.channel.delete.assert_not_awaited()
        app.notify_tutoring_user.assert_awaited_once()

    async def test_close_after_restart_restores_participants_and_is_idempotent(self):
        self.store.channels[200] = dict(applicant_id=20, tutor_id=30, status='active')
        interaction = self.interaction(channel_id=200)
        await app.CloseTutoringChannelView().close_channel.callback(interaction)
        await app.CloseTutoringChannelView().close_channel.callback(self.interaction(channel_id=200))
        self.channel.edit.assert_awaited_once()
        permissions = self.channel.edit.call_args.kwargs['overwrites']
        self.assertFalse(permissions[self.applicant].send_messages)
        self.assertFalse(permissions[self.tutor].send_messages)
        self.assertTrue(permissions[self.guild.me].send_messages)
        self.assertEqual(self.store.channels[200]['status'], 'closed')

    async def test_unrelated_member_cannot_close_channel(self):
        self.store.channels[200] = dict(applicant_id=20, tutor_id=30, status='active')
        await app.CloseTutoringChannelView().close_channel.callback(self.interaction(user=member(31), channel_id=200))
        self.channel.edit.assert_not_awaited()

    async def test_archive_permission_failure_keeps_channel_active(self):
        self.store.channels[200] = dict(applicant_id=20, tutor_id=30, status='active')
        self.channel.edit.side_effect = http_error()
        await app.CloseTutoringChannelView().close_channel.callback(self.interaction(channel_id=200))
        self.assertEqual(self.store.channels[200]['status'], 'active')

    async def test_archive_retains_member_overwrite_when_applicant_left(self):
        self.store.channels[200] = dict(applicant_id=20, tutor_id=30, status='active')
        self.guild.get_member.side_effect = lambda user_id: self.tutor if user_id == 30 else None
        await app.CloseTutoringChannelView().close_channel.callback(self.interaction(channel_id=200))
        permissions = self.channel.edit.call_args.kwargs['overwrites']
        target = next(target for target in permissions if target.id == 20)
        self.assertIs(target.type, discord.Member)
        self.assertFalse(permissions[target].send_messages)

    async def test_database_work_does_not_run_on_event_loop_thread(self):
        import threading
        main_thread = threading.get_ident()
        read = self.store.get_request
        threads = []

        def get_request(message_id):
            threads.append(threading.get_ident())
            return read(message_id)

        with patch.object(self.store, 'get_request', side_effect=get_request):
            await app.TutoringRequestView().decline.callback(self.interaction())
        self.assertTrue(threads)
        self.assertNotIn(main_thread, threads)

    async def test_setup_registers_stateless_persistent_views(self):
        with patch.object(app.bot, 'add_view') as add_view:
            await app.bot.setup_hook()
        self.assertEqual(add_view.call_count, 3)
        self.assertTrue(all(call.args[0].is_persistent() for call in add_view.call_args_list))

    async def test_submission_publishes_exactly_one_application(self):
        store = Mock()
        store.create_request.return_value = 10
        message = SimpleNamespace(id=100, edit=AsyncMock(), delete=AsyncMock())
        request_channel = SimpleNamespace(send=AsyncMock(return_value=message))
        self.guild.get_channel.return_value = request_channel
        interaction = self.interaction(user=self.applicant)
        modal = app.TutoringApplyModal()
        modal.agreement._value = '同意'
        modal.topic._value = 'Python'
        modal.schedule._value = '週末'
        with patch.object(app, 'tutoring_store', store):
            await modal.on_submit(interaction)
        request_channel.send.assert_awaited_once()
        store.attach_message.assert_called_once_with(10, 100)
        message.edit.assert_awaited_once()
        self.assertIsInstance(message.edit.call_args.kwargs['view'], app.TutoringRequestView)

    async def test_submission_failure_cleans_unpublished_record(self):
        store = Mock()
        store.create_request.return_value = 10
        self.guild.get_channel.return_value = SimpleNamespace(send=AsyncMock(side_effect=http_error()))
        modal = app.TutoringApplyModal()
        modal.agreement._value = '同意'
        modal.topic._value = 'Python'
        modal.schedule._value = '週末'
        with patch.object(app, 'tutoring_store', store):
            await modal.on_submit(self.interaction(user=self.applicant))
        store.discard_unpublished_request.assert_called_once_with(10)

    async def test_unresolved_tutor_does_not_silently_allow_any_tutor(self):
        store = Mock()
        self.guild.members = []
        modal = app.TutoringApplyModal()
        modal.agreement._value = '同意'
        modal.tutor._value = 'nonexistent'
        with patch.object(app, 'tutoring_store', store):
            await modal.on_submit(self.interaction(user=self.applicant))
        store.create_request.assert_not_called()

    async def test_submission_recovers_lost_message_id_commit_response(self):
        store = Mock()
        store.create_request.return_value = 10
        store.attach_message.side_effect = Error('commit response lost')
        store.get_request.return_value = dict(id=10)
        message = SimpleNamespace(id=100, edit=AsyncMock(), delete=AsyncMock())
        self.guild.get_channel.return_value = SimpleNamespace(send=AsyncMock(return_value=message))
        modal = app.TutoringApplyModal()
        modal.agreement._value = '同意'
        modal.topic._value = 'Python'
        modal.schedule._value = '週末'
        with patch.object(app, 'tutoring_store', store):
            await modal.on_submit(self.interaction(user=self.applicant))
        message.edit.assert_awaited_once()
        message.delete.assert_not_awaited()
        store.discard_unpublished_request.assert_not_called()

    async def test_submission_preserves_message_when_commit_cannot_be_confirmed(self):
        store = Mock()
        store.create_request.return_value = 10
        store.attach_message.side_effect = Error('commit response lost')
        store.get_request.side_effect = Error('DB offline')
        message = SimpleNamespace(id=100, edit=AsyncMock(), delete=AsyncMock())
        self.guild.get_channel.return_value = SimpleNamespace(send=AsyncMock(return_value=message))
        modal = app.TutoringApplyModal()
        modal.agreement._value = '同意'
        modal.topic._value = 'Python'
        modal.schedule._value = '週末'
        interaction = self.interaction(user=self.applicant)
        with patch.object(app, 'tutoring_store', store):
            await modal.on_submit(interaction)
        message.edit.assert_not_awaited()
        message.delete.assert_not_awaited()
        self.assertIn('核對', interaction.followup.send.call_args.args[0])

    async def test_closed_channel_removes_stale_button_even_if_edit_fails(self):
        self.store.channels[200] = dict(applicant_id=20, tutor_id=30, status='closed')
        for failed_edit in (False, True):
            interaction = self.interaction(channel_id=200)
            if failed_edit:
                interaction.message.edit.side_effect = http_error()
            await app.CloseTutoringChannelView().close_channel.callback(interaction)
            interaction.message.edit.assert_awaited_once_with(view=None)
            self.assertIn('已關閉', interaction.followup.send.call_args.args[0])
        self.channel.edit.assert_not_awaited()

    async def test_close_recovers_lost_commit_response(self):
        self.store.channels[200] = dict(applicant_id=20, tutor_id=30, status='active')
        close = self.store.close_channel

        def commit_then_disconnect(channel_id):
            close(channel_id)
            raise Error('commit response lost')

        interaction = self.interaction(channel_id=200)
        with patch.object(self.store, 'close_channel', side_effect=commit_then_disconnect), \
                patch.object(self.store, 'get_channel', wraps=self.store.get_channel) as read:
            await app.CloseTutoringChannelView().close_channel.callback(interaction)
        self.assertEqual(read.call_count, 2)
        self.assertIn('✅', interaction.followup.send.call_args.args[0])
        interaction.message.edit.assert_awaited_once_with(view=None)

    async def test_close_reports_unconfirmed_database_failure(self):
        record = dict(applicant_id=20, tutor_id=30, status='active')
        for confirmation in (record, None, Error('DB offline')):
            with self.subTest(confirmation=confirmation):
                interaction = self.interaction(channel_id=200)
                with patch.object(self.store, 'close_channel', side_effect=Error('commit failed')), \
                        patch.object(self.store, 'get_channel', side_effect=[record, confirmation]):
                    await app.CloseTutoringChannelView().close_channel.callback(interaction)
                self.assertIn('❌', interaction.followup.send.call_args.args[0])
                interaction.message.edit.assert_not_awaited()

    async def test_accept_reports_applicant_left_server(self):
        self.guild.get_member.return_value = None
        self.guild.get_member.side_effect = None
        self.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason='Not Found'), 'Unknown Member'))
        interaction = self.interaction()
        await app.TutoringRequestView().accept.callback(interaction)
        self.guild.create_text_channel.assert_not_awaited()
        self.assertIn('已離開伺服器', interaction.followup.send.call_args.args[0])
        self.assertEqual(self.store.request['status'], 'pending')

    async def test_decline_keeps_existing_applicant_mention_or_name(self):
        for display in ('<@20>', '@student'):
            with self.subTest(display=display):
                self.store.request['status'] = 'pending'
                interaction = self.interaction()
                interaction.message.embeds[0].description = f'申請人：{display}'
                await app.TutoringRequestView().decline.callback(interaction)
                embed = interaction.message.edit.call_args.kwargs['embed']
                self.assertIn(f'申請人：{display}', embed.description)
                self.assertIn('已婉拒', embed.description)

    async def test_decline_resolves_member_when_description_is_absent(self):
        interaction = self.interaction()
        await app.TutoringRequestView().decline.callback(interaction)
        embed = interaction.message.edit.call_args.kwargs['embed']
        self.assertIn(self.applicant.mention, embed.description)


class TutoringStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.rowcount = 1
        self.store = TutoringStore(lambda: self.connection)

    def test_acceptance_and_channel_insert_commit_together(self):
        channel = (200, 'test', 20, 'student', 30, 'tutor')
        self.assertTrue(self.store.finish_request(10, 'accepted', channel))
        self.assertEqual(self.cursor.execute.call_count, 2)
        self.connection.commit.assert_called_once()
        self.connection.rollback.assert_not_called()
        self.cursor.close.assert_called_once()
        self.connection.close.assert_called_once()

    def test_channel_insert_failure_rolls_back_acceptance(self):
        self.cursor.execute.side_effect = [None, Error('insert failed')]
        with self.assertRaises(Error):
            self.store.finish_request(10, 'accepted', (200, 'test', 20, 'student', 30, 'tutor'))
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()
        self.cursor.close.assert_called_once()
        self.connection.close.assert_called_once()

    def test_already_handled_request_does_not_insert_channel(self):
        self.cursor.rowcount = 0
        self.assertFalse(self.store.finish_request(10, 'accepted', (200, 'test', 20, 'student', 30, 'tutor')))
        self.assertEqual(self.cursor.execute.call_count, 1)

    def test_cursor_creation_failure_still_returns_connection(self):
        self.connection.cursor.side_effect = Error('cursor failed')
        with self.assertRaises(Error):
            self.store.get_request(100)
        self.connection.close.assert_called_once()

    def test_no_connection_is_reported_as_database_error(self):
        with self.assertRaises(Error):
            TutoringStore(lambda: None).get_request(100)

    def test_expiry_failure_rolls_back_entire_batch(self):
        self.cursor.fetchall.return_value = [dict(id=10), dict(id=11)]
        self.cursor.execute.side_effect = [None, Error('update failed')]
        with self.assertRaises(Error):
            self.store.expire_requests()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()
        self.connection.close.assert_called_once()

    def test_acceptance_without_channel_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.finish_request(10, 'accepted')
        self.connection.cursor.assert_not_called()

    def test_read_failure_never_rolls_back(self):
        self.cursor.execute.side_effect = Error('read failed')
        with self.assertRaisesRegex(Error, 'read failed'):
            self.store.get_request(100)
        self.connection.rollback.assert_not_called()
        self.cursor.close.assert_called_once()
        self.connection.close.assert_called_once()

    def test_dropped_connection_cleanup_preserves_original_error(self):
        original = Error('original operation failed')
        self.cursor.execute.side_effect = original
        self.connection.rollback.side_effect = Error('rollback failed')
        self.cursor.close.side_effect = Error('cursor close failed')
        self.connection.close.side_effect = Error('connection close failed')
        for write in (False, True):
            with self.subTest(write=write), self.assertRaises(Error) as caught:
                with self.store._cursor(write=write) as cursor:
                    cursor.execute('query')
            self.assertIs(caught.exception, original)
        self.connection.rollback.assert_called_once()
        self.assertEqual(self.connection.close.call_count, 2)

    def test_cursor_creation_failure_keeps_original_error_during_write(self):
        original = Error('cursor failed')
        self.connection.cursor.side_effect = original
        self.connection.rollback.side_effect = Error('rollback failed')
        self.connection.close.side_effect = Error('close failed')
        with self.assertRaises(Error) as caught:
            self.store.close_channel(200)
        self.assertIs(caught.exception, original)
        self.connection.close.assert_called_once()

    def test_cleanup_errors_do_not_turn_success_into_failure(self):
        self.cursor.fetchone.return_value = dict(id=10)
        self.cursor.close.side_effect = Error('close failed')
        self.connection.close.side_effect = Error('close failed')
        self.assertEqual(self.store.get_request(100), dict(id=10))
        self.connection.close.assert_called_once()

    def test_expiry_uses_one_conditional_batch_update(self):
        requests = [dict(id=10), dict(id=11), dict(id=12)]
        self.cursor.fetchall.return_value = requests
        self.assertEqual(self.store.expire_requests(), requests)
        self.assertEqual(self.cursor.execute.call_count, 2)
        query, params = self.cursor.execute.call_args.args
        self.assertIn('WHERE id IN (%s, %s, %s)', query)
        self.assertIn("AND status = 'pending'", query)
        self.assertEqual(params, (10, 11, 12))
        self.connection.commit.assert_called_once()

    def test_no_expired_requests_skips_update(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.store.expire_requests(), [])
        self.cursor.execute.assert_called_once()


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_dm_is_nonfatal(self):
        user = SimpleNamespace(send=AsyncMock(side_effect=http_error()))
        with patch.object(app.bot, 'get_user', return_value=user), patch.object(app, 'logger'):
            await app.notify_tutoring_user(20, 'accepted')

    async def test_expiry_is_committed_before_attempting_dm(self):
        request = dict(id=10, applicant_id=20, applicant_name='student',
                       topic='Python', message_id=100, created_at=datetime.now())
        store = Mock()
        store.expire_requests.return_value = [request]
        committed = []
        store.expire_requests.side_effect = lambda: committed.append(True) or [request]

        async def notify(*args, **kwargs):
            self.assertTrue(committed)

        with patch.object(app, 'tutoring_store', store), \
                patch.object(app.bot, 'get_channel', return_value=None), \
                patch.object(app, 'notify_tutoring_user', side_effect=notify), \
                patch.object(app, 'send_log', AsyncMock()):
            await app.check_expired_tutoring_requests.coro()

    async def test_abandoned_unpublished_requests_do_not_notify_applicants(self):
        store = Mock()
        store.expire_requests.return_value = [
            dict(id=10, applicant_id=20, message_id=None),
            dict(id=11, applicant_id=21, topic='Python', message_id=100),
        ]
        with patch.object(app, 'tutoring_store', store), \
                patch.object(app.bot, 'get_channel', return_value=None), \
                patch.object(app, 'notify_tutoring_user', AsyncMock()) as notify, \
                patch.object(app, 'send_log', AsyncMock()):
            await app.check_expired_tutoring_requests.coro()
        notify.assert_awaited_once()
        self.assertEqual(notify.call_args.args[0], 21)


class StartupTests(unittest.TestCase):
    def test_import_without_configuration_has_no_files_or_connections(self):
        root = str(Path(app.__file__).resolve().parent)
        script = f"""
import sys
sys.path.insert(0, {root!r})
from unittest.mock import patch
with patch('dotenv.load_dotenv') as dotenv, patch('mysql.connector.pooling.MySQLConnectionPool') as pool:
    import bot
    assert bot.db_pool is None
    dotenv.assert_not_called()
    pool.assert_not_called()
"""
        for config in ({}, {'ADMIN_CHANNEL_ID': 'invalid', 'LOG_CHANNEL_ID': 'invalid'}):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run([sys.executable, '-c', script], cwd=directory,
                                        env=config, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_startup_requires_settings_and_accepts_valid_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'ADMIN_CHANNEL_ID'):
                app.load_configuration(required=True)
        env = dict.fromkeys(CONFIG_IDS, '1')
        env['DISCORD_BOT_TOKEN'] = 'test-token'
        # Restore module settings after validating startup parsing.
        originals = {name: getattr(app, name) for name in (*CONFIG_IDS, 'LOG_CHANNEL_ID', 'DISCORD_BOT_TOKEN', 'DB_CONFIG')}
        self.addCleanup(lambda: app.__dict__.update(originals))
        with patch.dict(os.environ, env, clear=True):
            app.load_configuration(required=True)
            self.assertEqual(app.ADMIN_CHANNEL_ID, 1)
            self.assertEqual(app.LOG_CHANNEL_ID, 0)
            self.assertEqual(app.DISCORD_BOT_TOKEN, 'test-token')

    def test_file_logging_is_created_only_when_configured_and_is_idempotent(self):
        logger = app.logger
        handlers, level = list(logger.handlers), logger.level
        original_directory = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                os.chdir(directory)
                self.assertFalse(Path('logs/bot.log').exists())
                app.configure_logging()
                self.assertTrue(Path('logs/bot.log').exists())
                count = len(logger.handlers)
                app.configure_logging()
                self.assertEqual(len(logger.handlers), count)
                for handler in logger.handlers[:]:
                    if handler not in handlers:
                        logger.removeHandler(handler)
                        handler.close()
        finally:
            os.chdir(original_directory)
            logger.handlers[:] = handlers
            logger.setLevel(level)

    def test_daily_schedule_runs_at_ten_in_utc_plus_eight(self):
        self.assertIsNone(app.send_daily_schedule.hours)
        self.assertEqual(len(app.send_daily_schedule.time), 1)
        scheduled = app.send_daily_schedule.time[0]
        self.assertEqual((scheduled.hour, scheduled.minute), (10, 0))
        self.assertEqual(scheduled.utcoffset(), timedelta(hours=8))


class CommandRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = Mock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.return_value = None
        for item in (patch.object(app, 'get_db_connection', return_value=self.connection),
                     patch.object(app, 'send_log', AsyncMock()), patch.object(app, 'logger', Mock()), patch('builtins.print')):
            item.start()
            self.addCleanup(item.stop)

    def interaction(self, *, guild=True):
        return SimpleNamespace(
            user=member(30) if guild else SimpleNamespace(id=30),
            guild=Mock() if guild else None,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def writes(self):
        return (
            (app.add_class.callback, ('2026-11-25', '10:00', '11:00', 'Python', 'lesson')),
            (app.edit_class.callback, ('2026-11-25', '10:00')),
            (app.cancel_class.callback, ('2026-11-25', '10:00')),
        )

    async def test_guild_only_commands_reject_dm_before_accessing_member_fields(self):
        for callback, args in (*self.writes(), (app.setup_tag_preference.callback, ())):
            with self.subTest(command=callback.__name__):
                interaction = self.interaction(guild=False)
                await callback(interaction, *args)
                self.assertIn('伺服器', interaction.response.send_message.call_args.args[0])
        app.get_db_connection.assert_not_called()

    async def test_schedule_early_returns_close_each_resource_once(self):
        for callback, args in self.writes():
            for found in (False, True):
                with self.subTest(command=callback.__name__, found=found):
                    self.connection.reset_mock()
                    self.cursor.fetchone.return_value = dict(id=1, title='Python', etime='11:00', description='lesson') if found else None
                    await callback(self.interaction(), *args)
                    self.cursor.close.assert_called_once()
                    self.connection.close.assert_called_once()

    async def test_schedule_writes_commit_and_close_once(self):
        course = dict(id=1, title='Python', etime='11:00', description='lesson')
        for callback, args in self.writes():
            with self.subTest(command=callback.__name__):
                self.connection.reset_mock()
                self.cursor.fetchone.side_effect = [None] if callback is app.add_class.callback else [course, None]
                await callback(self.interaction(), *args)
                self.connection.commit.assert_called_once()
                self.cursor.close.assert_called_once()
                self.connection.close.assert_called_once()

    async def test_schedule_errors_are_generic_and_cleanup_survives_disconnect(self):
        commands = (*self.writes(), (app.today_schedule.callback, ()),
                    (app.view_schedule.callback, ('2026-11-25',)))
        for callback, args in commands:
            for cursor_failure in (False, True):
                with self.subTest(command=callback.__name__, cursor_failure=cursor_failure):
                    self.connection.reset_mock()
                    self.connection.cursor.side_effect = Error('private DB details') if cursor_failure else None
                    self.cursor.execute.side_effect = Error('private DB details')
                    self.connection.rollback.side_effect = Error('rollback disconnected')
                    self.cursor.close.side_effect = Error('cursor disconnected')
                    self.connection.close.side_effect = Error('connection disconnected')
                    interaction = self.interaction()
                    await callback(interaction, *args)
                    response = interaction.followup.send if callback in (app.today_schedule.callback, app.view_schedule.callback) else interaction.response.send_message
                    self.assertNotIn('private DB details', response.call_args.args[0])
                    self.assertIn('稍後重試', response.call_args.args[0])
                    self.connection.close.assert_called_once()
                    if not cursor_failure:
                        self.cursor.close.assert_called_once()

    async def test_daily_schedule_sends_without_checking_startup_hour(self):
        now = datetime(2026, 11, 25, 8, 30)
        self.cursor.fetchall.return_value = []
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(app, 'datetime') as clock, patch.object(app.bot, 'get_channel', return_value=channel):
            clock.now.return_value = now
            await app.send_daily_schedule.coro()
        channel.send.assert_awaited_once()
        self.assertIn('2026-11-25', channel.send.call_args.kwargs['embed'].title)
        self.cursor.close.assert_called_once()
        self.connection.close.assert_called_once()

    async def test_setup_tag_preference_does_not_expose_exception_details(self):
        interaction = self.interaction()
        interaction.user.guild_permissions = SimpleNamespace(administrator=True)
        channel = SimpleNamespace(send=AsyncMock(side_effect=Error('private DB details')))
        with patch.object(app.bot, 'get_channel', return_value=channel):
            await app.setup_tag_preference.callback(interaction)
        self.assertNotIn('private DB details', interaction.response.send_message.call_args.args[0])
        self.assertIn('稍後重試', interaction.response.send_message.call_args.args[0])

    async def test_tag_preferences_execute_off_thread_and_release_before_reply(self):
        main_thread = threading.get_ident()
        self.connection.cursor.side_effect = lambda **kwargs: self.assertNotEqual(threading.get_ident(), main_thread) or self.cursor
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                self.connection.reset_mock()
                interaction = self.interaction()

                async def reply(*args, **kwargs):
                    self.cursor.close.assert_called_once()
                    self.connection.close.assert_called_once()

                interaction.followup.send.side_effect = reply
                view = app.TagPreferenceView()
                await (view.accept_tag if accepted else view.decline_tag).callback(interaction)
                interaction.response.defer.assert_awaited_once()
                self.connection.commit.assert_called_once()
                self.assertIn(accepted, self.cursor.execute.call_args.args[1])

    async def test_tag_preferences_handle_cursor_and_cleanup_failures(self):
        for cursor_failure in (False, True):
            for accepted in (True, False):
                with self.subTest(cursor_failure=cursor_failure, accepted=accepted):
                    self.connection.reset_mock()
                    self.connection.cursor.side_effect = Error('original failure') if cursor_failure else None
                    self.cursor.execute.side_effect = Error('original failure')
                    self.connection.rollback.side_effect = Error('rollback disconnected')
                    self.cursor.close.side_effect = Error('cursor disconnected')
                    self.connection.close.side_effect = Error('connection disconnected')
                    interaction = self.interaction()
                    view = app.TagPreferenceView()
                    await (view.accept_tag if accepted else view.decline_tag).callback(interaction)
                    self.assertIn('設定失敗', interaction.followup.send.call_args.args[0])
                    self.connection.rollback.assert_called_once()
                    self.connection.close.assert_called_once()

    async def test_archive_cleanup_releases_db_before_delete_or_log(self):
        read_connection, write_connection = Mock(), Mock()
        read_cursor = read_connection.cursor.return_value
        write_cursor = write_connection.cursor.return_value
        read_cursor.fetchall.return_value = [dict(id=1, channel_id=200, channel_name='old')]
        channel = SimpleNamespace(delete=AsyncMock())

        async def delete():
            read_cursor.close.assert_called_once()
            read_connection.close.assert_called_once()
            write_connection.cursor.assert_not_called()

        async def log(*args):
            read_connection.close.assert_called_once()
            write_connection.close.assert_called_once()

        channel.delete.side_effect = delete
        with patch.object(app, 'get_db_connection', side_effect=[read_connection, write_connection]), \
                patch.object(app.bot, 'get_channel', return_value=channel), \
                patch.object(app, 'send_log', side_effect=log):
            await app.delete_archived_tutoring_channels.coro()
        channel.delete.assert_awaited_once()
        write_connection.commit.assert_called_once()
        write_cursor.close.assert_called_once()

    async def test_archive_cleanup_returns_connection_on_query_or_cursor_failure(self):
        for cursor_failure in (False, True):
            with self.subTest(cursor_failure=cursor_failure):
                self.connection.reset_mock()
                self.connection.cursor.side_effect = Error('cursor failed') if cursor_failure else None
                self.cursor.execute.side_effect = Error('select failed')
                self.cursor.close.side_effect = Error('cursor disconnected')
                self.connection.close.side_effect = Error('connection disconnected')
                with patch.object(app.bot, 'get_channel') as get_channel:
                    await app.delete_archived_tutoring_channels.coro()
                get_channel.assert_not_called()
                self.connection.close.assert_called_once()

    async def test_archive_update_failure_rolls_back_and_closes_connection(self):
        read_connection, write_connection = Mock(), Mock()
        read_connection.cursor.return_value.fetchall.return_value = [dict(id=1, channel_id=200, channel_name='old')]
        write_cursor = write_connection.cursor.return_value
        write_cursor.execute.side_effect = Error('update failed')
        write_connection.rollback.side_effect = Error('rollback disconnected')
        write_cursor.close.side_effect = Error('cursor disconnected')
        write_connection.close.side_effect = Error('connection disconnected')
        with patch.object(app, 'get_db_connection', side_effect=[read_connection, write_connection]), \
                patch.object(app.bot, 'get_channel', return_value=None):
            await app.delete_archived_tutoring_channels.coro()
        write_connection.commit.assert_not_called()
        write_connection.rollback.assert_called_once()
        write_cursor.close.assert_called_once()
        write_connection.close.assert_called_once()

    async def test_archive_delete_failure_does_not_mark_deleted(self):
        self.cursor.fetchall.return_value = [dict(id=1, channel_id=200, channel_name='old')]
        channel = SimpleNamespace(delete=AsyncMock(side_effect=http_error()))
        with patch.object(app.bot, 'get_channel', return_value=channel), \
                patch.object(app, 'mark_tutoring_channel_deleted') as mark:
            await app.delete_archived_tutoring_channels.coro()
        mark.assert_not_called()
        self.connection.close.assert_called_once()

    async def test_on_ready_handles_embed_without_title(self):
        user = SimpleNamespace(name='bot')

        async def history(**kwargs):
            yield SimpleNamespace(author=user, embeds=[discord.Embed(description='untitled')])

        channel = SimpleNamespace(name='tags', history=history, send=AsyncMock())
        with patch.object(app.bot._connection, 'user', user), \
                patch.object(app.bot, 'change_presence', AsyncMock()), \
                patch.object(app.bot, 'get_channel', return_value=channel), \
                patch.object(app.bot.tree, 'sync', AsyncMock(return_value=[])), \
                patch.object(app.check_expired_tutoring_requests, 'is_running', return_value=True), \
                patch.object(app.delete_archived_tutoring_channels, 'is_running', return_value=True), \
                patch.object(app.send_daily_schedule, 'is_running', return_value=True):
            await app.on_ready()
        channel.send.assert_awaited_once()


class ApplicationRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = SimpleNamespace(send=AsyncMock())
        for item in (patch.object(app, 'send_log', AsyncMock()), patch.object(app, 'logger', Mock()),
                     patch.object(app.bot, 'get_channel', return_value=self.channel),
                     patch.object(app, 'ADMIN_CHANNEL_ID', 1), patch('builtins.print')):
            item.start()
            self.addCleanup(item.stop)

    def interaction(self):
        return SimpleNamespace(
            user=SimpleNamespace(id=20, create_dm=AsyncMock()),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_modal_inputs_fit_embed_fields_and_live_topic_column(self):
        for modal_type in (app.IdentityChangeModal, app.PermissionChangeModal, app.TechIssueModal,
                           app.VolunteerContactModal, app.AnonymousReportModal):
            for field in modal_type().children:
                self.assertLessEqual(field.max_length, 1024)
        self.assertEqual(app.TutoringApplyModal().topic.max_length, 255)

    async def test_official_application_defers_before_network_and_uses_followup(self):
        interaction = self.interaction()

        async def fetch_user(user_id):
            interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            return SimpleNamespace(name='student')

        async def send(**kwargs):
            interaction.response.defer.assert_awaited_once()

        self.channel.send.side_effect = send
        with patch.object(app.bot, 'fetch_user', side_effect=fetch_user):
            await app.dispatch_official_application(interaction, dict(type='permission_change', discord_id='20', reason='reason'))
        interaction.followup.send.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()

    async def test_optional_discord_id_sets_accurate_anonymity(self):
        for modal_type in (app.TechIssueModal, app.VolunteerContactModal):
            for value, anonymous in (('', True), ('  ', True), ('匿名', True), ('20', False)):
                with self.subTest(modal=modal_type.__name__, value=value):
                    modal = modal_type()
                    modal.discord_id_input._value = value
                    with patch.object(app, 'dispatch_official_application', AsyncMock()) as dispatch:
                        await modal.on_submit(self.interaction())
                    data = dispatch.call_args.args[1]
                    self.assertEqual(data['is_anonymous'], anonymous)
                    self.assertEqual(data['discord_id'], '匿名' if anonymous else '20')

    async def test_rendered_official_fields_are_bounded_even_for_legacy_input(self):
        for app_type in ('identity_change', 'permission_change', 'tech_issue', 'volunteer_contact'):
            with self.subTest(app_type=app_type):
                with patch.object(app, 'get_user_display_name', AsyncMock(return_value='x' * 1900)):
                    await app.dispatch_official_application(self.interaction(), dict(
                        type=app_type, discord_id='20', reason='r' * 1900, issue='i' * 1900,
                        content='c' * 1900, document_link='d' * 1900))
                embed = self.channel.send.call_args.kwargs['embed']
                self.assertTrue(all(0 < len(field.value) <= 1024 for field in embed.fields))

    async def test_report_bounds_resolved_target_reason_and_combined_evidence(self):
        with patch.object(app, 'get_user_display_name', AsyncMock(return_value='target' * 400)):
            await app.dispatch_anonymous_report(self.interaction(), 'target', 'r' * 1900,
                                                additional_context='context' * 400)
        embed = self.channel.send.call_args.kwargs['embed']
        self.assertEqual([len(field.value) for field in embed.fields], [1024, 1024, 1024])

    async def test_attachment_timeout_handles_failed_dm_notification(self):
        interaction = self.interaction()
        dm = SimpleNamespace(send=AsyncMock(side_effect=[None, http_error()]))
        interaction.user.create_dm.return_value = dm
        modal = app.AttachmentUploadModal(dict(target_user='target', reason='reason'))
        with patch.object(app.bot, 'wait_for', AsyncMock(side_effect=asyncio.TimeoutError)), \
                patch.object(app, 'dispatch_anonymous_report', AsyncMock()) as dispatch:
            await modal.on_submit(interaction)
        dispatch.assert_not_awaited()
        self.assertIn('上傳超時', interaction.followup.send.call_args.args[0])

    async def test_attachment_unexpected_errors_and_cancellation_propagate(self):
        for error in (RuntimeError('unexpected'), asyncio.CancelledError()):
            interaction = self.interaction()
            interaction.user.create_dm.return_value = SimpleNamespace(send=AsyncMock())
            modal = app.AttachmentUploadModal(dict(target_user='target', reason='reason'))
            with patch.object(app.bot, 'wait_for', AsyncMock(side_effect=error)):
                with self.assertRaises(type(error)):
                    await modal.on_submit(interaction)
            interaction.followup.send.assert_not_awaited()

    async def test_attachment_success_confirmation_dm_failure_is_nonfatal(self):
        interaction = self.interaction()
        interaction.user.create_dm.return_value = SimpleNamespace(send=AsyncMock(side_effect=[None, http_error()]))
        modal = app.AttachmentUploadModal(dict(target_user='target', reason='reason', additional_context='evidence'))
        modal.note_input._value = 'n' * 1024
        with patch.object(app.bot, 'wait_for', AsyncMock(return_value=SimpleNamespace(attachments=[]))), \
                patch.object(app, 'get_user_display_name', AsyncMock(return_value='target')):
            await modal.on_submit(interaction)
        self.assertTrue(all(len(field.value) <= 1024 for field in self.channel.send.call_args.kwargs['embed'].fields))


if __name__ == '__main__':
    unittest.main()
