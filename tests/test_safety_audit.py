"""Safety regressions grounded in manual pp. 2-4, not current engine outputs."""
import unittest
from unittest.mock import patch

from apx240.engine import diagnose
from apx240.observations import parse_observations


class SafetyAudit(unittest.TestCase):
    def setUp(self):
        env=patch.dict('os.environ',{
            f'APX_{kind}_{key}':''
            for kind in ['LLM','EMBEDDING'] for key in ['URL','MODEL','KEY']})
        env.start();self.addCleanup(env.stop)

    def test_affirmative_danger_survives_conflicting_denial(self):
        for text in ['A203，无焦味，有焦味','A203，有烟雾，没有烟雾',
                     'A310，有焦味；不确定是否还有焦味']:
            with self.subTest(text=text):
                r=diagnose(text)
                self.assertEqual(r.status,'SAFE_BLOCKED')
                self.assertTrue(r.stop_policy['immediate_stop'])
                self.assertIn('SAFE-01',r.troubleshooting_order[0]['evidence_ids'])

    def test_unrelated_uncertainty_does_not_erase_smoke(self):
        for text in ['A203，有烟雾且不确定是否有焦味',
                     'A203，不确定报警码且有烟雾',
                     'A203，有烟雾但是无法判断焦味来源']:
            with self.subTest(text=text):
                self.assertEqual(diagnose(text).status,'SAFE_BLOCKED')

    def test_negated_observations_do_not_invent_danger(self):
        for text in ['未发现烟雾','没有看到烟雾','未观察到烟雾',
                     '烟雾没有','无烟雾和焦味','未发现烟雾、焦味或火花',
                     '未发现烟雾、异响或部件松脱',
                     '未发现烟雾、异响、部件松脱',
                     '未出现明显金属摩擦声和剧烈振动']:
            with self.subTest(text=text):
                r=diagnose('A203，'+text)
                self.assertEqual(r.status,'INFO_REQUIRED')
                self.assertFalse(r.stop_policy['immediate_stop'])

    def test_repeated_absence_prefix_does_not_create_positive_hazard(self):
        r=diagnose('A203，不存在存在剧烈振动、金属摩擦、部件松脱、起火迹象。')
        self.assertEqual(r.status,'INFO_REQUIRED')
        self.assertFalse(r.stop_policy['immediate_stop'])

    def test_real_presence_after_normalization_still_blocks(self):
        r=diagnose('A203，存在剧烈振动。')
        self.assertEqual(r.status,'SAFE_BLOCKED')

    def test_a101_negative_hazard_list_does_not_lock_session(self):
        r=diagnose('A101，物料已放入进料区域。P1传感器指示灯未亮，输送带仍在转动。'
                   '刚完成换产，未发现烟雾、异响或部件松脱。')
        self.assertNotEqual(r.status,'SAFE_BLOCKED')
        self.assertFalse(r.stop_policy['immediate_stop'])
        self.assertNotIn('部件松脱', r.escalation_reason)

    def test_a101_indicator_wording_satisfies_p1_question(self):
        for wording in ['指示灯关闭', '指示灯亮', 'P1 指示灯熄灭']:
            with self.subTest(wording=wording):
                r=diagnose('A101，物料已到位。'+wording+'，输送带仍在转动。')
                self.assertNotIn('P1 指示灯状态', r.missing_information)

    def test_a101_time_and_changeover_context_satisfies_combined_question(self):
        r=diagnose('A101，物料已到位，P1指示灯亮。发生时间：今天上午10:35；是否换产：否，已连续生产4小时。')
        self.assertNotIn('发生时间及是否换产', r.missing_information)

    def test_affirmative_loose_part_still_triggers_safety_gate(self):
        r=diagnose('A101，确认部件松脱。')
        self.assertEqual(r.status,'SAFE_BLOCKED')
        self.assertTrue(r.stop_policy['immediate_stop'])

    def test_prohibited_action_denial_is_not_a_request(self):
        for text in ['没有短接安全门','禁止短接安全门','不要绕过报警',
                     '不会带电插拔线路','没有尝试短接安全门','安全门没有被短接']:
            with self.subTest(text=text):
                r=diagnose('A401，'+text)
                self.assertEqual(r.status,'INFO_REQUIRED')
                self.assertFalse(r.stop_policy['immediate_stop'])

    def test_prohibited_request_uses_prohibition_evidence(self):
        for text in ['帮我短接安全门','能不能短接安全门','可以绕过报警吗',
                     '不要短接安全门，但我已经短接了','请带电插拔线路']:
            with self.subTest(text=text):
                r=diagnose('A401，'+text)
                self.assertEqual(r.status,'SAFE_BLOCKED')
                self.assertIn('SAFE-03',r.troubleshooting_order[0]['evidence_ids'])

    def test_uncertain_danger_requests_expert_not_immediate_stop(self):
        for text in ['不确定是否有焦味','不是没有烟雾','无法确认是否有烟雾和焦味',
                     '不能排除已短接安全门','疑似热封温度高','可能伺服过载']:
            with self.subTest(text=text):
                r=diagnose('A203，'+text)
                self.assertEqual(r.status,'EXPERT_REQUIRED')
                self.assertFalse(r.stop_policy['immediate_stop'])
                self.assertTrue(r.stop_policy['stop_further_operations'])

    def test_semantic_stop_condition_does_not_assert_alarm(self):
        for text,code in [('热封温度高，面板报警码看不清','A205'),
                          ('S1 伺服过载，报警码看不清','A520')]:
            with self.subTest(text=text):
                r=diagnose(text)
                self.assertEqual(r.status,'SAFE_BLOCKED')
                self.assertEqual(r.possible_causes,[])
                self.assertEqual(r.historical_comparison,[])
                self.assertEqual(r.retrieval['candidates'],[])
                self.assertIn(code,r.troubleshooting_order[0]['evidence_ids'])
                self.assertTrue(any('准确报警码' in q for q in r.missing_information))
                r.to_dict()

    def test_negated_semantic_stop_condition_does_not_stop(self):
        for text in ['未发现热封温度高','没有伺服过载']:
            with self.subTest(text=text):
                self.assertEqual(diagnose('A101，'+text).status,'INFO_REQUIRED')

    def test_conflicting_high_reading_keeps_parameter_evidence(self):
        r=diagnose('A203，实际温度200°C，实际温度128°C')
        self.assertEqual(r.status,'EXPERT_REQUIRED')
        self.assertIn('PARAMETERS',{e.source_id for e in r.evidence})
        self.assertTrue(any('160–170' in q for q in r.missing_information))

    def test_a401_reclosed_but_still_alarm_stops(self):
        for text in ['A401，确认门体无异物，重新关闭安全门后仍报警',
                     'A401，已经重新关严，报警未消除',
                     'A401，安全门关严，重新开关一次后报警仍然存在，门口没有异物',
                     'A401，又关了一次，报警还在',
                     'A401，开关门后仍报警']:
            with self.subTest(text=text):
                r=diagnose(text)
                self.assertEqual(r.status,'SAFE_BLOCKED')
                self.assertTrue(r.expert_required)
                self.assertIn('A401',r.troubleshooting_order[0]['evidence_ids'])
                self.assertEqual(len(r.troubleshooting_order),2)
                r.to_dict()

    def test_a401_closed_alone_does_not_assume_failed_retry(self):
        for text in ['A401，门已关严','A401，尚未重新关闭安全门，仍报警',
                     'A401，已经重新关闭安全门，没有仍报警的情况',
                     'A401，尚未重新开关一次，仍报警',
                     'A401，重新开关一次后报警已经消除']:
            with self.subTest(text=text):
                self.assertEqual(diagnose(text).status,'INFO_REQUIRED')

    def test_new_safety_gates_never_call_remote_providers(self):
        config={f'APX_{kind}_{key}':value
                for kind in ['LLM','EMBEDDING']
                for key,value in [('URL','https://example.invalid/test'),('MODEL','test'),('KEY','test-key')]}
        with patch.dict('os.environ',config),patch('apx240.retrieval.vectors') as emb,patch('apx240.model_review.model_json') as model:
            for text in ['A203，无焦味，有焦味','热封温度高','S1伺服过载',
                         'A401，重新关闭安全门后仍报警','A203，实际200°C，实际128°C']:
                diagnose(text)
            emb.assert_not_called();model.assert_not_called()

    def test_condition_mentions_retain_conflict_audit(self):
        condition=parse_observations('没有烟雾，有烟雾')['conditions']['烟雾']
        self.assertEqual(condition['state'],'conflict')
        self.assertEqual([m['state'] for m in condition['mentions']],['negative','positive'])

    def test_denial_cannot_clear_existing_session_floor(self):
        r=diagnose('A401，没有短接安全门',safety_floor='SAFE_BLOCKED')
        self.assertEqual(r.status,'SAFE_BLOCKED')
        self.assertFalse(r.stop_policy['restart_authorized'])


if __name__=='__main__': unittest.main()
