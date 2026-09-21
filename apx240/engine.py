import re, unicodedata, uuid, time
from .knowledge import Knowledge
from .models import FaultReport
from .retrieval import retrieve, REPAIR_ALARMS
from .observations import (parse_observations, single_value, compare_history,
    HAZARDS as HAZARD_GROUPS, LABELS, PROHIBITED_TERMS, SEMANTIC_STOP_ALARMS, has_affirmative)
from .model_review import review

ASSESSMENT_ORDER={'PRIORITY':0,'RETAINED':1,'CURRENTLY_UNSUPPORTED':2}

def assess_causes(causes,selected,observations):
    """Stratify manual candidates without deleting or confirming any cause."""
    for cause in causes:
        cause['assessment']='RETAINED'
        cause['assessment_label']='保留候选'
        cause['assessment_reason']='说明书列出该原因；当前信息不足以提高或降低核对顺序'
    by_id={c['cause_id']:c for c in causes}
    def mark(cid,level,reason):
        if cid in by_id:
            by_id[cid]['assessment']=level
            by_id[cid]['assessment_label']={'PRIORITY':'优先核查','RETAINED':'保留候选','CURRENTLY_UNSUPPORTED':'当前证据不支持'}[level]
            by_id[cid]['assessment_reason']=reason
    actual=single_value(observations,'actual_temperature')
    setting=single_value(observations,'set_temperature')
    current=single_value(observations,'heater_current')
    if 'A203' in selected:
        preheated=observations['conditions']['preheated']['state']
        if preheated=='positive':mark('A203-C1','CURRENTLY_UNSUPPORTED','现场已说明完成预热，当前信息不支持“未预热”')
        elif preheated=='negative':mark('A203-C1','PRIORITY','现场明确尚未完成预热，应先核对预热条件')
        if setting is not None:
            mark('A203-C2','CURRENTLY_UNSUPPORTED' if 160<=setting<=170 else 'PRIORITY',
                 '设定温度处于说明书稳定范围' if 160<=setting<=170 else '设定温度不在说明书稳定范围，应优先核对')
        if current==0 and actual is not None and (setting is None or actual<setting):
            mark('A203-C3','PRIORITY','实际温度偏低且加热电流为0A，与加热回路未输出相符，但仍需授权人员测量确认')
            mark('A203-C4','PRIORITY','0A也可能来自继电器或线路路径，需与加热器开路区分')
    if 'A310' in selected:
        upstream=single_value(observations,'upstream_pressure')
        device=single_value(observations,'device_pressure')
        leak=observations['conditions']['leak']['state']
        if upstream is not None:
            mark('A310-C1','PRIORITY' if upstream<0.5 else 'CURRENTLY_UNSUPPORTED',
                 '上游压力低于说明书入口范围' if upstream<0.5 else '上游压力处于说明书入口范围')
        if upstream is not None and device is not None and device<upstream:
            mark('A310-C2','PRIORITY','设备端压力低于上游压力，应优先核对过滤和调压路径')
            mark('A310-C3','PRIORITY','设备端压力低于上游压力，应优先核对过滤和调压路径')
        if leak=='positive':mark('A310-C4','PRIORITY','现场明确报告漏气声')
        elif leak=='negative':mark('A310-C4','CURRENTLY_UNSUPPORTED','现场明确未发现漏气声，当前信息不支持明显泄漏')
    if 'A101' in selected:
        material=observations['conditions']['material_present']['state']
        if material=='negative':mark('A101-C1','PRIORITY','现场明确物料未到位')
        elif material=='positive':mark('A101-C1','CURRENTLY_UNSUPPORTED','现场明确物料已到位')
        p1_off=bool(re.search(r'P1\s*(?:指示)?灯(?:不亮|熄灭)',observations['current_text']))
        if p1_off:mark('A101-C2','PRIORITY','物料状态需结合P1指示灯不亮优先核对传感器位置或遮挡')
    if 'A401' in selected and observations['conditions']['door_closed']['state']=='positive':
        mark('A401-C1','CURRENTLY_UNSUPPORTED','现场已说明安全门关严')
    return sorted(causes,key=lambda c:ASSESSMENT_ORDER[c['assessment']])

def choose_next_question(status,questions,selected):
    if status=='SAFE_BLOCKED' or not questions:return {}
    topic_priority={
        'A203':['是否完成预热','设定温度','加热电流','实际温度'],
        'A310':['上游压力','设备端压力','是否有漏气声'],
        'A101':['物料是否到位','P1 指示灯状态'],
        'A401':['夹料','异物','重新关闭安全门'],
        'A900':['受影响模块','面板状态'],
    }
    ranked=[]
    for index,question in enumerate(questions):
        score=0;topic=''
        for code in selected:
            for rank,keyword in enumerate(topic_priority.get(code,[])):
                if keyword in question and 100-rank>score:
                    score=100-rank;topic=keyword
        if any(k in question for k in ['风险尚不确定','请确认是否存在']):score=max(score,60)
        ranked.append((-score,index,question,topic))
    _,_,question,topic=min(ranked)
    if topic:reason=f'“{topic}”能直接区分当前报警路径中的多个手册候选原因'
    elif any(k in question for k in ['风险','烟雾','焦味','火花','异常高温']):reason='该信息直接决定下一条安全处置路径'
    else:reason='该答案最有助于缩小当前保留的候选范围'
    return {'question':question,'reason':reason,'remaining_count':max(0,len(questions)-1)}

def safety_signals(text, observations=None):
    obs=observations or parse_observations(text)
    return sorted(h for h in {**HAZARD_GROUPS,**PROHIBITED_TERMS,**SEMANTIC_STOP_ALARMS}
                  if has_affirmative(obs['conditions'][h]))

def diagnose(description, kb=None, *, safety_floor=None):
    started=time.perf_counter()
    if not isinstance(description,str) or not description.strip() or len(description)>8000:
        raise ValueError('请输入 1–8000 字的故障描述')
    kb=kb or Knowledge()  # verify snapshot every request
    if safety_floor not in [None,'SAFE_BLOCKED','EXPERT_REQUIRED']:
        raise ValueError('Invalid safety floor')
    text=unicodedata.normalize('NFKC',description).upper()
    def form_value(label):
        match=re.search(r'(?:^|\n)'+re.escape(label)+r'\s*[:：]\s*([^\n]+)',description,re.I)
        return match.group(1).strip() if match and match.group(1).strip() not in ['未填写','未知'] else None
    field_context={
        'equipment_id':form_value('设备编号'), 'production_line':form_value('产线/工位'),
        'fault_occurred_at':form_value('故障时间'), 'reporter':form_value('报告人'),
        'occurrence_stage':form_value('发生阶段')}
    codes=list(dict.fromkeys(re.findall(r'(?<![A-Z0-9])A\s*(\d{3})(?![A-Z0-9])',text)))
    codes=['A'+c for c in codes]
    observations=parse_observations(description)
    current_codes=['A'+c for c in re.findall(r'(?<![A-Z0-9])A\s*(\d{3})(?![A-Z0-9])',observations['current_text'])]
    codes=list(dict.fromkeys(current_codes))
    ambiguous_codes=(len(codes)>1 and bool(re.search(r'(?:可能|疑似|也可能|看不清|无法确认|不确定)[^。；\n]{0,50}A\s*\d{3}',text)))
    if ambiguous_codes:
        codes=[]
    if observations['excluded_historical_clauses'] and not re.search(r'(?:本次|现在|当前)[^，,。；;\n]*A\s*\d{3}',observations['current_text']):
        # Alarm routing is uncertain when the description mixes past and present alarms.
        observations['conflicts'].append('输入包含历史描述；请单独确认本次报警与读数，暂不混用历史信息')
    issues=list(observations['conflicts'])
    if ambiguous_codes:
        issues.append('报警码存在多个不确定读数，请现场核对清晰代码')
    issues.extend(h+'风险尚不确定' for h in {**HAZARD_GROUPS,**PROHIBITED_TERMS,**SEMANTIC_STOP_ALARMS}
                  if observations['conditions'][h]['state'] in ['uncertain','conflict'])
    actual=single_value(observations,'actual_temperature')
    # Conflicting readings do not remove a high reading from risk review.
    high_temperature=any(v['value']>170 for v in observations['measurements'].get('actual_temperature',[]))
    if high_temperature:
        issues.append('实际温度超过手册 160–170°C 稳定范围，风险需专家核对')
    signals=safety_signals(text,observations)
    semantic_stop_codes=[code for label,code in SEMANTIC_STOP_ALARMS.items() if label in signals]
    door_retry=observations['conditions']['door_reclosed']
    persistent_alarm=observations['conditions']['alarm_persists']
    if 'A401' in codes and has_affirmative(door_retry) and has_affirmative(persistent_alarm):
        signals.append('安全门重新关闭后仍报警')
        semantic_stop_codes.append('A401')
    selected,retrieval=retrieve(text,kb,codes,allow_remote=not(safety_floor or signals or issues or any(c in codes for c in ['A205','A520'])))
    evidence=[]
    def rule(r):
        e=kb.evidence(r,'safety_rule',3,'2. 安全红线',kb.rules[r])
        if e not in evidence: evidence.append(e)
    for r in ['SAFE-02','SAFE-03','SAFE-04']: rule(r)
    if high_temperature:
        evidence.append(kb.evidence('PARAMETERS','manual',2,'1. 设备概述与正常参数',kb.pages[1]))
    blocked=bool(signals) or 'A205' in selected or 'A520' in selected or safety_floor=='SAFE_BLOCKED'
    if safety_floor:
        issues.append('本会话先前已触发安全阻断或专家复核，补充信息不会自动解除')
    unknown=bool(retrieval['unknown_codes'])
    unsupported=not selected and not retrieval['candidates']
    causes=[]; steps=[]; questions=[]; comparisons=[]
    def alarm_evidence(c):
        a=kb.alarms[c]
        page=kb.pages[3]
        start=page.index(c)
        following=[page.find(other,start+len(c)) for other in kb.alarms if other!=c]
        end=min([p for p in following if p>=0] or [len(page)])
        e=kb.evidence(c,'alarm_manual',4,a['section'],page[start:end].strip())
        if e not in evidence: evidence.append(e)
    # A clear stop-condition phrase supports a stop policy, never an inferred
    # exact alarm, candidate cause, history match, or executable diagnostic path.
    for c in semantic_stop_codes: alarm_evidence(c)
    if not selected:
        for candidate in retrieval['candidates']: alarm_evidence(candidate['code'])
    for c in selected:
        a=kb.alarms[c]
        alarm_evidence(c)
        causes.extend({'cause_id':f'{c}-C{i+1}','description':'可能：'+cause,'evidence_ids':[c],'confirmed':False,'ranking_basis':'手册列举顺序，未作概率估计'} for i,cause in enumerate(a['causes']))
        steps.extend({'action':s,'evidence_ids':[c],'executor':'授权维修人员' if any(k in s for k in ['清洁','校准','锁定','隔离','泄压','专家','授权','传动','外部连接']) else '现场人员（仅外部观察/面板读取）'} for s in a['steps'])
        questions.extend(a['questions'])
        if c=='A401':
            if not re.search(r'(?:门框|门边|门体)[^，。；]{0,12}(?:无|没有|未见|未发现)(?:夹料|异物)|(?:无|没有|未见|未发现)[^，。；]{0,12}(?:夹料|异物)',observations['current_text']):
                questions.append('请确认门框和门体是否夹料或存在异物')
            if not has_affirmative(observations['conditions']['door_reclosed']):
                questions.append('清除异物后是否已重新关闭安全门')
        for h in kb.history:
            if h['alarm']==c:
                history_page=kb.pages[4];hs=history_page.index(h['id'])
                he=min([p for other in kb.history if other['id']!=h['id'] for p in [history_page.find(other['id'],hs+len(h['id']))] if p>=0] or [len(history_page)])
                evidence.append(kb.evidence(h['id'],'history_case',5,h['section'],history_page[hs:he].strip(),True))
                comparison=compare_history(observations,h)
                comparisons.append({'source_id':h['id'],**comparison,
                    'historical_observation':h['observation'],'historical_cause':h['cause'],
                    'current_observation':description})
                preferred={'H02':'A203-C3','H03':'A310-C4'}.get(h['id'])
                if preferred and len(comparison['same'])>1 and not comparison['differences'] and not comparison['unknown']:
                    for candidate in causes:
                        if candidate['cause_id']==preferred:
                            candidate['evidence_ids'].append(h['id'])
                            candidate['ranking_basis']='当前已提取读数与历史相同，仅提高候选优先级，未确认根因'
                            causes.remove(candidate);causes.insert(0,candidate);break
                m=next(m for m in kb.repairs if REPAIR_ALARMS.get(m['id'])==c)
                evidence.append(kb.evidence(m['id'],'maintenance_record',6,m['section'],m['cause'],True))
                comparisons.append({'source_id':m['id'],'same':['与该报警模块相关的历史维修候选'],
                    'historical_cause':m['cause'],'current_observation':description,
                    'differences_or_unknown':'原维修记录未声明报警码；按模块关联。本次部件状态未经验证，维修后验证值不代表当前读数。'})
    supplied={LABELS[k] for k in LABELS if single_value(observations,k) is not None}
    if observations['conditions']['material_present']['state'] in ['positive','negative']:supplied.add('物料是否到位')
    if re.search(r'(?:P1\s*)?(?:指示)?灯(?:不亮|亮|熄灭|关闭)',observations['current_text']):supplied.add('P1 指示灯状态')
    # A101 asks for one combined context item. Treat it as answered only when
    # both the occurrence time and changeover status are present; otherwise keep
    # the question visible so the operator is not led to believe it is complete.
    if (re.search(r'(?:发生时间|故障时间|出现时间)\s*[:：]?\s*[^，。；;\n]+', observations['current_text'])
            and re.search(r'(?:是否换产|刚?换产|换产)\s*[:：]?\s*[^，。；;\n]+', observations['current_text'])):
        supplied.add('发生时间及是否换产')
    for key,label in [('leak','是否有漏气声'),('preheated','是否完成预热')]:
        if observations['conditions'][key]['state'] in ['positive','negative']: supplied.add(label)
    questions=[q for q in dict.fromkeys(questions) if q not in supplied]
    questions.extend(issues)
    if not selected: questions.insert(0,'请提供面板准确报警码、出现时间及当前读数')
    if unknown: questions.insert(0,'请核对未收录报警码：'+', '.join(retrieval['unknown_codes']))
    missing_safety=[h for h in HAZARD_GROUPS if observations['conditions'][h]['state']=='unknown']
    if not signals and missing_safety: questions.append('确认是否存在'+ '、'.join(missing_safety)+'；未提及不代表无风险')
    expert=blocked or unknown or unsupported or bool(issues) or any(c in selected for c in ['A203','A900'])
    if 'A310' in selected and single_value(observations,'upstream_pressure') is not None and single_value(observations,'device_pressure') is not None and observations['conditions']['leak']['state']=='positive':
        expert=True
    reason='涉及授权维修时升级；当前先补充信息并完成安全的外部观察。'
    if blocked:
        rule('SAFE-01')
        if unknown or ambiguous_codes: rule('SAFE-05')
        reason='安全阻断：'+('、'.join(signals) if signals else '报警码要求停机及专家检查')
        # Global safety gate replaces all runnable diagnostic sequences, including multiple alarms.
        stop_ids=(['SAFE-01'] if any(h in signals for h in HAZARD_GROUPS) or safety_floor=='SAFE_BLOCKED' else [])
        if any(h in signals for h in PROHIBITED_TERMS): stop_ids.append('SAFE-03')
        stop_ids=list(dict.fromkeys(stop_ids+[c for c in selected if c in ['A205','A520']]+semantic_stop_codes+(['SAFE-05'] if unknown or ambiguous_codes else [])))
        if safety_floor=='SAFE_BLOCKED': reason='保留本会话先前的立即停机要求；需授权人员复核，不能自动放行。'
        steps=[{'action':'立即停机并升级专家；不得继续试运行。','evidence_ids':stop_ids,'executor':'现场人员'},
               {'action':kb.rules['SAFE-02'],'evidence_ids':['SAFE-02'],'executor':'授权维修人员'}]
    elif unknown or unsupported or issues:
        rule('SAFE-05'); reason=('；'.join(issues) if issues else '知识库无覆盖')+'，停止进一步操作并升级专家。'
        steps=[{'action':kb.rules['SAFE-05'],'evidence_ids':['SAFE-05'],'executor':'专家'}]
    elif expert: reason='排查涉及加热回路、电气或控制专家；只生成建议，授权人员执行。'
    # No-code candidate retrieval never silently asserts an alarm.
    if not selected and retrieval['candidates']:
        questions.append('候选方向：'+ '、'.join(kb.alarms[c['code']]['meaning'] for c in retrieval['candidates'])+'；请确认报警码后生成排查顺序')
    status='SAFE_BLOCKED' if blocked else 'EXPERT_REQUIRED' if (unknown or unsupported or issues) else 'INFO_REQUIRED' if questions else 'REPORT_READY'
    rid=str(uuid.uuid4())
    report=FaultReport(rid,'APX-240',kb.manifest['version_id'],status,description,
        [{'text':description,'source':'现场输入（未经独立核实）'}],causes,evidence,
        [kb.rules[r] for r in ['SAFE-02','SAFE-03','SAFE-04']],steps,questions,expert,reason,comparisons,retrieval,
        ['PARSE','SAFETY_PRECHECK','RETRIEVE','EVIDENCE_VERIFY',status],{},observations)
    report.model_review,model_data=review(report)
    if model_data:
        by_id={c['cause_id']:c for c in report.possible_causes}
        report.possible_causes=[by_id[item['cause_id']] for item in model_data['ranking']]
        for c in report.possible_causes: c['ranking_basis']='模型在已引用证据范围内排序；可能原因，未确认'
        if model_data['risk']!='no_additional_risk':
            rule('SAFE-05')
            report.status='EXPERT_REQUIRED';report.expert_required=True
            report.escalation_reason='模型标记风险待专家核对；引用现场：'+model_data['risk_quote']
            report.troubleshooting_order=[{'action':kb.rules['SAFE-05'],'evidence_ids':['SAFE-05'],'executor':'专家'}]
    report.possible_causes=assess_causes(report.possible_causes,selected,observations)
    report.next_best_question=choose_next_question(report.status,report.missing_information,selected)
    report.trace.extend(['MODEL_'+report.model_review['status'].upper(),'FINAL_SAFETY_VERIFY',report.status])
    report.stop_policy={
        'immediate_stop':report.status=='SAFE_BLOCKED',
        'stop_further_operations':report.status in ['SAFE_BLOCKED','EXPERT_REQUIRED'],
        'isolation_before_intrusive_work':True,
        'restart_authorized':False,
        'explanation':'立即停机' if report.status=='SAFE_BLOCKED' else '停止进一步操作并转专家' if report.status=='EXPERT_REQUIRED' else '仅按手册做外部观察；接触、拆装或清理前必须停机隔离。本报告不授权运行或复机。'}
    for e in report.evidence:
        e.authority=('设备说明书' if e.source_type in ['alarm_manual','manual'] else
                     '安全规则' if e.source_type=='safety_rule' else '历史候选')
        e.usage=('直接支持报警含义、候选原因或排查步骤' if e.source_type=='alarm_manual' else
                 '约束安全边界和升级路径' if e.source_type=='safety_rule' else
                 '支持设备参数范围' if e.source_type=='manual' else
                 '仅用于调整候选优先级，不确认本次根因')
    evidence_by_id={e.source_id:e for e in report.evidence}
    for cause in report.possible_causes:
        sources=[evidence_by_id[x] for x in cause['evidence_ids']]
        cause['evidence_level']='说明书直接支持' if any(e.source_type=='alarm_manual' for e in sources) else '历史候选'
        cause['verification_status']='待现场验证'
    report.execution_metadata={'trace_id':rid,'knowledge_snapshot_digest':kb.snapshot_digest,
        'policy_path':report.status,'retrieval_channel':report.retrieval.get('semantic_channel',report.retrieval.get('channel','unknown')),
        'model_status':report.model_review.get('status','unknown'),
        'duration_ms':round((time.perf_counter()-started)*1000,2)}
    report.work_order={'work_order_id':'DRAFT-'+rid[:8],'status':'DRAFT','status_label':'草稿，待人工审核','equipment':'APX-240',
        'report_id':rid,'summary':description,'risk_status':report.status,'expert_required':report.expert_required,
        'knowledge_version':report.knowledge_version,'confirmed_root_cause':None,
        'proposed_steps':report.troubleshooting_order,'missing_information':questions,'evidence_ids':[e.source_id for e in evidence],
        'external_sync':'未提交飞书','restart_approved':False,
        'equipment_id':field_context['equipment_id'],'production_line':field_context['production_line'],
        'fault_occurred_at':field_context['fault_occurred_at'],'reporter':field_context['reporter'],
        'occurrence_stage':field_context['occurrence_stage'],
        'assigned_role':'设备专家' if report.expert_required else '现场工程师',
        'actions_taken':[],'parts_used':[],'verification_conditions':[],
        'status_history':[{'status':'DRAFT','label':'草稿，待人工审核','actor':'系统','note':'由诊断报告生成；未确认根因或复机权限'}]}
    report.work_order['stop_policy']=report.stop_policy
    ids={e.source_id for e in evidence}
    if any(not set(item['evidence_ids'])<=ids for item in report.possible_causes+report.troubleshooting_order):
        raise ValueError('报告存在无来源条目')
    return report
