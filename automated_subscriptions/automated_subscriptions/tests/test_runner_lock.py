"""The party lock primitive: site-scoped filelock, two threads, no DB access from the threads."""

import contextvars
import threading
import time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from automated_subscriptions.automated_subscriptions.billing.runner import party_lock_name

LOCK = party_lock_name("_Test Company", "Customer", "_Test Lock Customer")


def run_in_thread(fn, *args):
	"""frappe.local is a contextvar-backed werkzeug Local: copy the context so the thread sees the site."""
	ctx = contextvars.copy_context()
	thread = threading.Thread(target=ctx.run, args=(fn, *args))
	thread.start()
	return thread


class TestRunnerLock(IntegrationTestCase):
	def test_lock_name_is_a_bare_path_component(self):
		self.assertRegex(LOCK, r"^subbill-[0-9a-f]{16}$")
		self.assertEqual(LOCK, party_lock_name("_Test Company", "Customer", "_Test Lock Customer"))
		self.assertNotEqual(LOCK, party_lock_name("_Test Company", "Customer", "_Test Lock Customer / X"))

	def test_filelock_serialises_two_threads(self):
		events = []
		errors = []

		def holder():
			try:
				with filelock(LOCK, timeout=5):
					events.append("A in")
					time.sleep(0.3)
					events.append("A out")
			except Exception as e:
				errors.append(e)

		def waiter():
			try:
				with filelock(LOCK, timeout=5):
					events.append("B in")
					events.append("B out")
			except Exception as e:
				errors.append(e)

		with patch("frappe.utils.synchronization.frappe.log_error"):
			a = run_in_thread(holder)
			time.sleep(0.05)
			b = run_in_thread(waiter)
			a.join(10)
			b.join(10)

		self.assertEqual(errors, [])
		self.assertEqual(events, ["A in", "A out", "B in", "B out"])

	def test_filelock_timeout_raises(self):
		timeouts = []
		errors = []
		released = threading.Event()

		def holder():
			try:
				with filelock(LOCK, timeout=5):
					time.sleep(0.5)
			except Exception as e:
				errors.append(e)
			finally:
				released.set()

		def waiter():
			try:
				with filelock(LOCK, timeout=0.1):
					pass
			except LockTimeoutError as e:
				timeouts.append(e)
			except Exception as e:
				errors.append(e)

		with patch("frappe.utils.synchronization.frappe.log_error") as log_error:
			a = run_in_thread(holder)
			time.sleep(0.05)
			b = run_in_thread(waiter)
			b.join(10)
			a.join(10)

		self.assertTrue(released.is_set())
		self.assertEqual(errors, [])
		self.assertEqual(len(timeouts), 1)
		self.assertIsInstance(timeouts[0], LockTimeoutError)
		self.assertNotIsInstance(timeouts[0], frappe.ValidationError)  # why the runner wraps it
		log_error.assert_called_once()
