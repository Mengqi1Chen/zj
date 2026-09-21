"""Local review registration, without verified identity or restart authorization."""
import copy
import json
import uuid
from .sessions import SessionConflict,SessionNotFound,timestamp
from .knowledge import Knowledge

class ReviewStore:
    def __init__(self,sessions):
        self.sessions=sessions
        with sessions.connect(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS reviews(session_id TEXT PRIMARY KEY,state TEXT NOT NULL)')

    def _session(self,db,session_id):
        row=db.execute('SELECT state FROM sessions WHERE id=?',(session_id,)).fetchone()
        if not row:raise SessionNotFound('会话不存在')
        return json.loads(row[0])

    def _view(self,review,session):
        data=copy.deepcopy(review)
        data['outdated']=review['report_revision']!=session['revision']
        data['current_report_revision']=session['revision']
        try:data['knowledge_current']=review['packet']['knowledge_snapshot_digest']==Knowledge().snapshot_digest
        except Exception:data['knowledge_current']=False
        data['outdated']=data['outdated'] or not data['knowledge_current']
        data['identity_verified']=False
        data['restart_authorized']=False
        labels={'SUBMITTED':'已提交复核','IN_REVIEW':'专家复核中','NEEDS_INFORMATION':'待补充现场信息','REVIEW_RECORDED':'复核意见已登记'}
        data['work_order_projection']={
            'work_order_id':review['packet']['report']['work_order']['work_order_id'],
            'status':review['status'],'status_label':labels[review['status']],
            'assigned_to':review.get('assignee'),'report_revision':review['report_revision'],
            'restart_approved':False,
            'status_history':[{'status':e['action'].upper(),'actor_self_reported':e['actor_self_reported'],
                               'note':e['note'],'at':e['at']} for e in review['events']]}
        data['notice']='本地复核登记，姓名由填写者自报，未核验身份；不构成专家签署或复机许可。'
        return data

    def get(self,session_id):
        with self.sessions.connect() as db:
            session=self._session(db,session_id)
            row=db.execute('SELECT state FROM reviews WHERE session_id=?',(session_id,)).fetchone()
            return self._view(json.loads(row[0]),session) if row else None

    def apply(self,session_id,action,actor,note,questions,expected_revision,expected_review_revision,request_id):
        if action not in ['submit','claim','request_information','record_review']:
            raise ValueError('不支持该复核动作；本版本不能批准复机')
        if not isinstance(actor,str) or len(actor.strip())>80:raise ValueError('登记人姓名最多80字')
        if not isinstance(note,str) or len(note.strip())>2000:raise ValueError('登记说明最多2000字')
        if not isinstance(questions,list) or len(questions)>10 or any(not isinstance(q,str) or not 1<=len(q.strip())<=300 for q in questions):raise ValueError('待补信息最多10项，每项1–300字')
        if action=='request_information' and not questions:raise ValueError('退回补充信息时至少填写一项问题')
        if action in ['claim','record_review'] and questions:raise ValueError('开始复核或保存意见时不应填写待补问题')
        if type(expected_revision) is not int or expected_revision<1 or type(expected_review_revision) is not int or expected_review_revision<0:raise ValueError('复核版本无效')
        payload={'operation':'review','session_id':session_id,'action':action,'actor':actor,'note':note,'questions':questions,'expected_revision':expected_revision,'expected_review_revision':expected_review_revision}
        fingerprint=self.sessions._fingerprint(payload)
        with self.sessions.lock,self.sessions.connect(write=True) as db:
            cached=self.sessions._retry(db,request_id,fingerprint)
            if cached:return cached
            session=self._session(db,session_id)
            if session.get('knowledge_snapshot_digest')!=Knowledge().snapshot_digest:
                raise SessionConflict('当前知识快照与会话不一致，须人工核对后新建诊断；旧复核包可导出')
            row=db.execute('SELECT state FROM reviews WHERE session_id=?',(session_id,)).fetchone()
            review=json.loads(row[0]) if row else None
            if session['revision']!=expected_revision or (review['revision'] if review else 0)!=expected_review_revision:
                raise SessionConflict('报告或复核记录已更新，请载入最新版本后提交')
            if review and len(review['events'])>=100:raise ValueError('复核记录已达100次上限，请导出并人工整理')
            if action=='submit':
                if not actor.strip():raise ValueError('提交复核时请填写登记人姓名')
                if not note.strip():raise ValueError('提交复核时请填写复核说明')
                if review and review['report_revision']==session['revision']:
                    raise SessionConflict('当前报告版本已进入复核，无需重复提交')
                if not session.get('knowledge_snapshot_digest'):raise SessionConflict('旧报告缺完整知识快照，须人工核对后新建会话')
                review=review or {'review_id':str(uuid.uuid4()),'session_id':session_id,'revision':0,'events':[],'submissions':[]}
                packet={'report_revision':session['revision'],'knowledge_snapshot_digest':session['knowledge_snapshot_digest'],
                        'report':copy.deepcopy(session['latest_report']),'turns':copy.deepcopy(session['turns']),
                        'safety_latch':session['safety_latch'],'latch_events':copy.deepcopy(session['latch_events'])}
                review.update(status='SUBMITTED',report_revision=session['revision'],assignee=None,questions=questions,packet=packet)
                review['submissions'].append(copy.deepcopy(packet))
            else:
                if not review:raise SessionConflict('请先登记待复核报告')
                if review['report_revision']!=session['revision']:raise SessionConflict('现场信息已更新，须重新提交当前报告后复核')
                transitions={'claim':('SUBMITTED','IN_REVIEW'),'request_information':('IN_REVIEW','NEEDS_INFORMATION'),'record_review':('IN_REVIEW','REVIEW_RECORDED')}
                before,after=transitions[action]
                if review['status']!=before:raise SessionConflict('当前复核状态不允许该动作')
                if action=='claim':
                    if not actor.strip():raise ValueError('开始复核时请填写复核人员')
                    if not note.strip():note='已开始复核。'
                elif action=='request_information':
                    actor=actor.strip() or review.get('assignee','')
                    if not actor:raise ValueError('请先开始复核并登记复核人员')
                    if not note.strip():note='退回补充现场信息。'
                elif action=='record_review':
                    actor=actor.strip() or review.get('assignee','')
                    if not actor:raise ValueError('请先开始复核并登记复核人员')
                    if not note.strip():raise ValueError('保存复核意见时请填写复核说明')
                review['status']=after
                if action=='claim':review['assignee']=actor.strip()
                review['questions']=questions
            review['revision']+=1
            review['events'].append({'revision':review['revision'],'report_revision':session['revision'],'action':action,
                                    'actor_self_reported':actor.strip(),'note':note.strip(),'questions':questions,'at':timestamp()})
            result=self._view(review,session)
            db.execute('INSERT INTO reviews VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET state=excluded.state',(session_id,json.dumps(review,ensure_ascii=False)))
            db.execute('INSERT INTO requests VALUES(?,?,?)',(request_id,fingerprint,json.dumps(result,ensure_ascii=False)))
            return result
