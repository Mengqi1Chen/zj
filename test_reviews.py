import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from apx240.sessions import SessionStore,SessionConflict
from apx240.reviews import ReviewStore
from apx240.evaluate import offline_environment

class Reviews(unittest.TestCase):
    def setUp(self):
        ctx=offline_environment();ctx.__enter__();self.addCleanup(ctx.__exit__,None,None,None)
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=SessionStore(Path(tmp.name)/'test.db');self.reviews=ReviewStore(self.store)
        self.session=self.store.create('A520，有金属摩擦声','s1');self.sid=self.session['session_id']

    def action(self,action,revision,report_revision=1,request=None,questions=None):
        return self.reviews.apply(self.sid,action,'测试登记人','测试登记说明',questions or [],report_revision,revision,request or action+str(revision))

    def test_complete_registration_never_releases_safety(self):
        self.action('submit',0);self.action('claim',1);review=self.action('record_review',2)
        self.assertEqual(review['status'],'REVIEW_RECORDED')
        self.assertEqual(review['work_order_projection']['status'],'REVIEW_RECORDED')
        self.assertFalse(review['work_order_projection']['restart_approved'])
        self.assertFalse(review['identity_verified']);self.assertFalse(review['restart_authorized'])
        self.assertEqual(self.store.get(self.sid),self.session)
        self.assertEqual(review['packet']['report']['status'],'SAFE_BLOCKED')

    def test_information_resubmit_and_snapshot_retained(self):
        self.action('submit',0);self.action('claim',1);self.action('request_information',2,questions=['发生时间？'])
        self.store.append(self.sid,'上午10点发生','append',1,'s2')
        self.assertTrue(self.reviews.get(self.sid)['outdated'])
        with self.assertRaises(SessionConflict):self.action('record_review',3,report_revision=2)
        review=self.action('submit',3,report_revision=2)
        self.assertEqual(len(review['submissions']),2)
        self.assertEqual(review['submissions'][0]['report_revision'],1)
        self.assertEqual(review['status'],'SUBMITTED');self.assertFalse(review['outdated'])

    def test_cannot_skip_claim(self):
        self.action('submit',0)
        with self.assertRaises(SessionConflict):self.action('record_review',1)

    def test_cannot_release_or_sign(self):
        for action in ['approve_restart','release','sign']:
            with self.subTest(action=action),self.assertRaises(ValueError):self.action(action,0)

    def test_idempotency_across_instances(self):
        first=self.action('submit',0)
        self.reviews=ReviewStore(SessionStore(self.store.path))
        second=self.action('submit',0)
        self.assertEqual(first,second);self.assertEqual(len(second['events']),1)

    def test_stale_report_and_review_rejected(self):
        self.action('submit',0)
        with self.assertRaises(SessionConflict):self.action('claim',0)
        self.store.append(self.sid,'现在没有异常','append',1,'s2')
        with self.assertRaises(SessionConflict):self.action('claim',1)
        with self.assertRaises(SessionConflict):self.action('claim',1,report_revision=2)

    def test_needs_information_requires_questions(self):
        self.action('submit',0);self.action('claim',1)
        with self.assertRaises(ValueError):self.action('request_information',2)
        self.assertEqual(self.reviews.get(self.sid)['revision'],2)

    def test_submit_can_include_initial_questions(self):
        result=self.action('submit',0,questions=['加热器两端电压是多少？'])
        self.assertEqual(result['status'],'SUBMITTED')
        self.assertEqual(result['questions'],['加热器两端电压是多少？'])

    def test_duplicate_submit_rejected(self):
        self.action('submit',0)
        with self.assertRaises(SessionConflict):self.action('submit',1)

    def test_invalid_request_does_not_mutate_session(self):
        with self.assertRaises(ValueError):self.reviews.apply(self.sid,'submit','','note',[],1,0,'bad')
        self.assertIsNone(self.reviews.get(self.sid))
        self.assertEqual(self.store.get(self.sid),self.session)

    def test_old_packet_unaffected_by_new_revision(self):
        submitted=self.action('submit',0)
        self.store.append(self.sid,'补充现场信息','append',1,'s2')
        self.assertEqual(self.reviews.get(self.sid)['packet'],submitted['packet'])

    def test_request_identifier_cannot_be_reused_for_different_action(self):
        self.action('submit',0,request='fixed')
        with self.assertRaises(SessionConflict):self.action('claim',1,request='fixed')

    def test_changed_knowledge_cannot_be_reviewed_as_current(self):
        self.action('submit',0)
        with patch('apx240.reviews.Knowledge') as kb:
            kb.return_value.snapshot_digest='changed'
            self.assertTrue(self.reviews.get(self.sid)['outdated'])
            with self.assertRaises(SessionConflict):self.action('claim',1)
