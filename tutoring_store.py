"""Tutoring persistence. Call these blocking operations with asyncio.to_thread."""

from contextlib import contextmanager

from mysql.connector import Error


class TutoringStore:
    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    @contextmanager
    def _cursor(self, *, write=False):
        connection = self.connection_factory()
        if connection is None:
            raise Error("無法連接資料庫，請稍後重試。")
        cursor = None
        try:
            cursor = connection.cursor(dictionary=True, buffered=True)
            yield cursor
            if write:
                connection.commit()
        except Exception:
            if write:
                try:
                    connection.rollback()
                except Error:
                    pass
            raise
        finally:
            try:
                if cursor is not None:
                    cursor.close()
            except Error:
                pass
            finally:
                try:
                    connection.close()
                except Error:
                    pass

    def create_request(self, applicant, topic, preferred_tutor, schedule):
        with self._cursor(write=True) as cursor:
            cursor.execute(
                """INSERT INTO tutoring_requests
                   (applicant_id, applicant_name, topic, preferred_tutor_id,
                    preferred_tutor_name, schedule, created_at, status, message_id)
                   VALUES (%s, %s, %s, %s, %s, %s, NOW(), 'pending', NULL)""",
                (applicant.id, applicant.display_name, topic,
                 preferred_tutor.id if preferred_tutor else None,
                 preferred_tutor.display_name if preferred_tutor else None, schedule),
            )
            return cursor.lastrowid

    def attach_message(self, request_id, message_id):
        with self._cursor(write=True) as cursor:
            cursor.execute(
                "UPDATE tutoring_requests SET message_id = %s WHERE id = %s",
                (message_id, request_id),
            )

    def discard_unpublished_request(self, request_id):
        with self._cursor(write=True) as cursor:
            cursor.execute(
                """DELETE FROM tutoring_requests
                   WHERE id = %s AND status = 'pending' AND message_id IS NULL""",
                (request_id,),
            )

    def get_request(self, message_id):
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT id, applicant_id, preferred_tutor_id, status,
                          created_at <= DATE_SUB(NOW(), INTERVAL 24 HOUR) AS expired
                   FROM tutoring_requests WHERE message_id = %s""",
                (message_id,),
            )
            return cursor.fetchone()

    def finish_request(self, request_id, status, channel=None):
        if status not in ('accepted', 'declined'):
            raise ValueError("Unsupported request status")
        if (status == 'accepted') != (channel is not None):
            raise ValueError("Accepted requests must include a channel record")
        with self._cursor(write=True) as cursor:
            cursor.execute(
                """UPDATE tutoring_requests SET status = %s, closed_at = NOW()
                   WHERE id = %s AND status = 'pending'
                     AND created_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)""",
                (status, request_id),
            )
            if cursor.rowcount != 1:
                return False
            if channel is not None:
                cursor.execute(
                    """INSERT INTO tutoring_channels
                       (channel_id, channel_name, applicant_id, applicant_name,
                        tutor_id, tutor_name, status)
                       VALUES (%s, %s, %s, %s, %s, %s, 'active')""",
                    channel,
                )
            return True

    def get_channel(self, channel_id):
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT applicant_id, tutor_id, status FROM tutoring_channels
                   WHERE channel_id = %s""",
                (channel_id,),
            )
            return cursor.fetchone()

    def close_channel(self, channel_id):
        with self._cursor(write=True) as cursor:
            cursor.execute(
                """UPDATE tutoring_channels SET status = 'closed', closed_at = NOW()
                   WHERE channel_id = %s AND status = 'active'""",
                (channel_id,),
            )
            return cursor.rowcount == 1

    def expire_requests(self):
        # The row lock and conditional update keep expiry from overwriting acceptance.
        with self._cursor(write=True) as cursor:
            cursor.execute(
                """SELECT id, applicant_id, applicant_name, topic, message_id, created_at
                   FROM tutoring_requests
                   WHERE status = 'pending'
                     AND created_at <= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                   FOR UPDATE"""
            )
            requests = cursor.fetchall()
            if requests:
                placeholders = ', '.join(['%s'] * len(requests))
                cursor.execute(
                    f"""UPDATE tutoring_requests SET status = 'expired', closed_at = NOW()
                        WHERE id IN ({placeholders}) AND status = 'pending'""",
                    tuple(request['id'] for request in requests),
                )
            return requests
