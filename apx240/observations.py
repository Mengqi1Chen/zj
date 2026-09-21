"""Deterministic current-observation extraction. Unknown is not false."""
import math
import re
import unicodedata

LABELS = {'set_temperature':'设定温度','actual_temperature':'实际温度','heater_current':'加热电流',
          'upstream_pressure':'上游压力','device_pressure':'设备端压力'}
MEASUREMENTS = {
    'set_temperature': (r'(?:设定(?:温度)?|目标温度)', '°C'),
    'actual_temperature': (r'(?:实际(?:温度)?|实测温度|当前温度)', '°C'),
    'heater_current': (r'(?:加热)?电流', 'A'),
    'upstream_pressure': (r'上游(?:气压|压力)?', 'MPa'),
    'device_pressure': (r'(?:设备端|设备入口|入口)(?:气压|压力)?', 'MPa'),
}
HAZARDS = {
    '烟雾':['烟雾','冒烟','浓烟'], '焦味':['焦味','烧焦味','烧糊味','糊味'],
    '异常高温':['异常高温','烫得厉害'], '剧烈振动':['剧烈振动','剧烈震动','震得厉害','振得厉害','抖得厉害'],
    '金属摩擦':['金属摩擦声','金属摩擦','摩擦声','金属刮擦'], '部件松脱':['部件松脱','零件脱落','松脱'],
    '起火迹象':['火花','着火','起火'],
}
# These phrases carry the manual's stop requirement even when a display code is
# unreadable. They never establish an alarm or a root cause for retrieval.
SEMANTIC_STOP_ALARMS = {'热封温度高':'A205', '伺服过载':'A520'}
SEMANTIC_STOP_TERMS = {'热封温度高':['热封温度高','热封温度过高','热封温度偏高','热封过热'],
                       '伺服过载':['伺服过载','伺服过负荷']}
PROHIBITED_TERMS = {'短接安全门':['短接'], '绕过保护':['绕过','屏蔽报警'],
                    '带电操作':['带电拆','带电插拔'], '徒手触碰加热部件':['徒手触碰'],
                    '强行运行':['强行运行']}
BOOL_TERMS = {**HAZARDS,**SEMANTIC_STOP_TERMS,**PROHIBITED_TERMS,
              # Keep common non-stop observations in the condition vocabulary so
              # negation can span lists such as “未发现烟雾、异响或部件松脱”.
              # This is deliberately not a HAZARDS entry: an unqualified
              # abnormal-sound report must not itself trigger the safety gate.
              'abnormal_sound':['异响','异常声音','异常噪声','异常响声'],
              'leak':['漏气声'], 'preheated':['完成预热','预热完成'],
              'door_closed':['门已关严','门关严','门已关闭','门已经关严','门已经关闭'],
              'door_reclosed':['重新关闭安全门','重新关闭','重新关门','重新关严','重新合上安全门','再次关门','再次关闭',
                               '重关安全门','重新开关安全门','重新开关一次','开关门后','重新开合','开合一次','又关了一次',
                               '重新合上','重新合上门','打开又重新合上','先打开又重新合上'],
              'alarm_persists':['仍然报警','仍报警','报警未消除','报警还在','报警仍存在','报警仍然存在',
                                '报警依旧没有消失','报警依旧未消失','报警还是没有消失','报警没有消失'],
              'material_present':['物料已到位','物料到位'], 'p1_lit':['P1灯亮','P1 灯亮']}
# Common field-report denials. Keep verb forms such as “未出现” here so a
# negation can scope over a following enumeration: “未出现明显金属摩擦声和剧烈振动”.
NEG = r'(?:没有|没|未见|未发现|未观察到|未看到|未出现|未发生|未产生|未引发|不存在|无|未曾|从未|尚未|未|禁止|严禁|不要|避免|不允许|不应该|不应|不打算|不会|不能|不)'
UNCERTAIN = r'无法|不知道|不确定|不清楚|有没有|可能|疑似|似乎|好像|是否|并非|不是|不能排除|未排除|不排除'
KNOWN_CONDITION = '|'.join(re.escape(t) for t in sorted({t for ts in BOOL_TERMS.values() for t in ts},key=len,reverse=True))

def normalize(text):
    return unicodedata.normalize('NFKC', text).upper()

def boolean_observation(text, terms):
    mentions=[]
    pattern='|'.join(re.escape(t) for t in sorted(terms,key=len,reverse=True))
    for clause in re.split(r'[，,。；;\n]|但是|不过|但', text):
        for m in re.finditer(pattern,clause):
            prefix=clause[:m.start()]
            suffix=clause[m.end():]
            prior=list(re.finditer(KNOWN_CONDITION,prefix))
            local_prefix=prefix[prior[-1].end():] if prior else prefix
            local_prefix=re.split(r'而且|并且|以及|同时|且|并',local_prefix)[-1]
            # Scope uncertainty to this condition, so "有烟雾且不确定是否有焦味"
            # cannot erase the affirmative smoke report.
            state='positive'
            if re.search(r'能不能|可不可以',local_prefix) and any(term in terms for values in PROHIBITED_TERMS.values() for term in values):
                state='positive'
            elif re.search(UNCERTAIN,local_prefix) or re.match(r'\s*(?:吗|么|是否|不确定|不能排除|未排除|无法确认|\?|？)',suffix):
                state='uncertain'
            elif re.search(NEG+r'(?:看到|看见|观察到|发现|明显|持续|已经|已|进行|有|任何|尝试|被)?\s*$',local_prefix):
                state='negative'
            elif re.match(r'\s*(?:为|是)?\s*(?:没有|未见|未发现|不存在|未出现|未发生|无)(?:\s|$|[，,。；;])',suffix):
                state='negative'
            else:
                # Carry a leading negative operator across a plain enumeration,
                # including the final "或火花" item.  The clause splitter above
                # already ends this scope at 但是/不过/但.
                shared_enum=re.fullmatch(r'\s*'+NEG+r'(?:看到|看见|观察到|发现|明显|持续|任何|有)?\s*(?:(?:'+KNOWN_CONDITION+r')\s*(?:和|及|或|、|以及|与)?\s*)*',prefix)
                if shared_enum:
                    state='negative'
                    mentions.append({'state':state,'quote':clause.strip()})
                    continue
                # Share an operator only across a plain condition enumeration.
                # A new assertion such as "无烟雾且有焦味" ends its scope.
                enum=re.search(r'((?:'+KNOWN_CONDITION+r')(?:和|及|或|、)(?:(?:'+KNOWN_CONDITION+r')(?:和|及|或|、))*)$',prefix)
                if enum:
                    shared=prefix[:enum.start()]
                    if re.search(UNCERTAIN,shared): state='uncertain'
                    elif re.search(NEG+r'(?:看到|看见|观察到|发现|明显|持续|已经|已|有)?\s*$',shared): state='negative'
            mentions.append({'state':state,'quote':clause.strip()})
    states={m['state'] for m in mentions}
    state='unknown' if not states else 'conflict' if {'positive','negative'}<=states else 'uncertain' if 'uncertain' in states else 'positive' if 'positive' in states else 'negative'
    return {'state':state,'mentions':mentions}

def has_affirmative(observation):
    """A later denial or an uncertain mention cannot erase explicit danger."""
    return any(m['state']=='positive' for m in observation['mentions'])

def parse_observations(description):
    text=normalize(description)
    # Do not import a user-quoted past case into current measurements or safety negation.
    current=[]; historical=[]
    past=False
    for clause in re.split(r'[，,。；;\n]|(?=现在|本次|当前)',text):
        if re.search(r'现在|本次|当前|现场',clause): past=False
        if re.search(r'历史|以前|上次|维修记录|案例|之前',clause): past=True
        (historical if past else current).append(clause)
    text='，'.join(current)
    readings={}; conflicts=[]
    for key,(label,unit) in MEASUREMENTS.items():
        units=r'(?:°\s*C|摄氏度|度|C)' if unit=='°C' else r'A(?![A-Z0-9])' if unit=='A' else r'(?:MPA|KPA|BAR)'
        pattern=label+r'\s*(?:读数|显示|记录)?\s*(?:为|是|只有|仅有|[:：=])?\s*(-?\d+(?:\.\d+)?)\s*('+units+r')'
        values=[]
        for m in re.finditer(pattern,text):
            v=float(m.group(1)); raw_unit=m.group(2)
            if raw_unit=='KPA': v/=1000
            elif raw_unit=='BAR': v/=10
            values.append({'value':v,'unit':unit,'quote':m.group(0),'source':'现场输入'})
        if values: readings[key]=values
        if len({round(v['value'],8) for v in values})>1:
            conflicts.append(LABELS[key]+'存在不同读数，需明确测点与时间')
        if any(v['value']<0 for v in values) and unit in ['MPa','A']:
            conflicts.append(LABELS[key]+'出现负值，量程或单位待核对')
    booleans={key:boolean_observation(text,terms) for key,terms in BOOL_TERMS.items()}
    if re.search(r'无法确定哪|读数.*(?:矛盾|冲突)|同一.*(?:一处|两处|另一处)',text):
        conflicts.append('读数描述存在矛盾或无法确定，需核对同一测点与时间')
    if re.search(r'设备型号[^，。；]{0,20}(?:未知|不清楚|不知道|未确认)',text):
        conflicts.append('当前设备型号未确认，不能套用 APX-240 知识')
    conflicts.extend(key+'存在肯定和否定描述' for key,b in booleans.items() if b['state']=='conflict')
    models=list(dict.fromkeys(re.findall(r'APX\s*[-–]?\s*(\d{3})',text)))
    if any(m!='240' for m in models): conflicts.append('设备型号不属于 APX-240 知识库')
    return {'measurements':readings,'conditions':booleans,'conflicts':conflicts,'excluded_historical_clauses':historical,'current_text':text,
            'equipment_mentions':['APX-'+m for m in models]}

def single_value(observation, field):
    values=observation['measurements'].get(field,[])
    distinct={round(v['value'],8) for v in values}
    return values[0]['value'] if len(distinct)==1 else None

def compare_history(current, history):
    old=parse_observations(history['observation'])
    same=['报警码 '+history['alarm']]; different=[]; unknown=[]
    for key,items in old['measurements'].items():
        value=single_value(current,key); expected=items[0]['value']; unit=items[0]['unit']
        if value is None: unknown.append(LABELS[key]+'未提供或存在冲突')
        elif math.isclose(value,expected,rel_tol=0,abs_tol=1e-6): same.append(f'{LABELS[key]} {value:g} {unit}')
        else: different.append(f'{LABELS[key]}：当前 {value:g} {unit}，历史 {expected:g} {unit}')
    for key,b in old['conditions'].items():
        if b['state']=='unknown': continue
        now=current['conditions'][key]['state']
        name={'leak':'漏气声','door_closed':'门关严','material_present':'物料到位','p1_lit':'P1 灯亮'}.get(key,key)
        if now in ['unknown','uncertain','conflict']: unknown.append(name+'未确认')
        elif now==b['state']: same.append(name+'状态相同')
        else: different.append(name+'与历史不一致')
    return {'same':same,'differences':different,'unknown':unknown,
            'differences_or_unknown':'；'.join(different+unknown) or '已提取的读数和条件相同；仍不能确认本次根因。'}
