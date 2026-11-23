import json
from .source import record
from .store import update

TOL=1e-6


def _r(x):return round(float(x),3)


def _factor(db,meter_id,at):
    row=db.execute('SELECT factor FROM solvent_calibration WHERE meter_id=? AND effective_from<=? ORDER BY effective_from DESC,issued_at DESC LIMIT 1',(meter_id,at)).fetchone()
    return row['factor'] if row else 1.0


def _measured(db,liters,meter_id,at):
    return _r(liters*_factor(db,meter_id,at))


def _route_ok(db,route_id,kind,at):
    return db.execute('SELECT 1 FROM solvent_route WHERE route_id=? AND kind=? AND valid_from<=? AND valid_to>=?',(route_id,kind,at,at)).fetchone() is not None


def _barrel_ids(db):
    ids=set()
    for table,column in [('solvent_barrel','barrel_id'),('solvent_draw','barrel_id'),('solvent_reflux','barrel_id'),('solvent_handover','barrel_id'),('solvent_stock_check','barrel_id')]:
        ids|={row[0] for row in db.execute(f'SELECT DISTINCT {column} FROM {table}')}
    ids|={row[0] for row in db.execute('SELECT DISTINCT input_barrel_id FROM solvent_distillation')}
    ids|={row[0] for row in db.execute('SELECT DISTINCT output_barrel_id FROM solvent_distillation')}
    return ids


def _operation_flows(db):
    ops={}
    for draw in db.execute('SELECT * FROM solvent_draw'):
        flow=ops.setdefault(draw['operation_id'],{'drawn':0.0,'refluxed':0.0,'by_barrel':{}})
        amount=_measured(db,draw['liters'],draw['meter_id'],draw['at'])
        flow['drawn']=_r(flow['drawn']+amount)
        flow['by_barrel'][draw['barrel_id']]=_r(flow['by_barrel'].get(draw['barrel_id'],0.0)+amount)
    for reflux in db.execute('SELECT * FROM solvent_reflux'):
        flow=ops.setdefault(reflux['operation_id'],{'drawn':0.0,'refluxed':0.0,'by_barrel':{}})
        flow['refluxed']=_r(flow['refluxed']+_measured(db,reflux['liters'],reflux['meter_id'],reflux['at']))
    return ops


def _barrel_evaporation(ops):
    evaporation={}
    for flow in ops.values():
        total=flow['drawn']
        loss=max(0.0,_r(total-flow['refluxed']))
        if total>TOL:
            for barrel_id,amount in flow['by_barrel'].items():
                evaporation[barrel_id]=_r(evaporation.get(barrel_id,0.0)+loss*amount/total)
    return evaporation


def _decide_barrel(db,barrel_id,evaporation):
    declarations=db.execute('SELECT * FROM solvent_barrel WHERE barrel_id=? ORDER BY event_id',(barrel_id,)).fetchall()
    roles={row['role'] for row in declarations}
    purchased=_r(sum(_measured(db,row['liters'],row['meter_id'],row['at']) for row in declarations if row['role']=='purchase'))
    conversion_ok=all(abs(row['weight_kg']/row['density_kg_per_l']-row['liters'])<=0.01 for row in declarations if row['role']=='purchase' and row['density_kg_per_l'])
    reflux_in=_r(sum(_measured(db,row['liters'],row['meter_id'],row['at']) for row in db.execute('SELECT * FROM solvent_reflux WHERE barrel_id=?',(barrel_id,))))
    regenerated=0.0
    distilled=0.0
    residue=0.0
    still_loss=0.0
    unapproved=0
    for still in db.execute('SELECT * FROM solvent_distillation'):
        if not _route_ok(db,still['route_id'],'distill',still['at']):
            if barrel_id in (still['input_barrel_id'],still['output_barrel_id']):unapproved+=1
            continue
        input_l=_measured(db,still['input_liters'],still['meter_id'],still['at'])
        reusable_l=_measured(db,still['reusable_liters'],still['meter_id'],still['at'])
        residue_l=_measured(db,still['residue_liters'],still['meter_id'],still['at'])
        if still['input_barrel_id']==barrel_id:
            distilled=_r(distilled+input_l)
            residue=_r(residue+residue_l)
            still_loss=_r(still_loss+max(0.0,_r(input_l-reusable_l-residue_l)))
        if still['output_barrel_id']==barrel_id:regenerated=_r(regenerated+reusable_l)
    drawn=_r(sum(_measured(db,row['liters'],row['meter_id'],row['at']) for row in db.execute('SELECT * FROM solvent_draw WHERE barrel_id=?',(barrel_id,))))
    handed=0.0
    in_transit=0.0
    transferred=0.0
    transfer_loss=0.0
    for handover in db.execute('SELECT * FROM solvent_handover WHERE barrel_id=?',(barrel_id,)):
        if not _route_ok(db,handover['route_id'],'outbound',handover['at']):
            unapproved+=1
            continue
        amount=_measured(db,handover['liters'],handover['meter_id'],handover['at'])
        handed=_r(handed+amount)
        receipts=db.execute('SELECT * FROM solvent_waste_receipt WHERE handover_id=? ORDER BY at,event_id',(handover['event_id'],)).fetchall()
        weighed=[r for r in receipts if r['status']=='weighed' and r['received_liters'] is not None]
        if weighed:
            received=weighed[-1]['received_liters']
            transferred=_r(transferred+received)
            transfer_loss=_r(transfer_loss+max(0.0,_r(amount-received)))
        else:
            in_transit=_r(in_transit+amount)
    in_l=_r(purchased+reflux_in+regenerated)
    out_l=_r(drawn+distilled+handed)
    remaining=_r(in_l-out_l)
    stock_gap=0.0
    surplus=False
    check=db.execute('SELECT * FROM solvent_stock_check WHERE barrel_id=? ORDER BY at DESC,event_id DESC LIMIT 1',(barrel_id,)).fetchone()
    if check:
        gap=_r(remaining-_measured(db,check['measured_liters'],check['meter_id'],check['at']))
        if gap>TOL:stock_gap=gap
        elif gap<-TOL:surplus=True
    unexplained=_r(still_loss+transfer_loss+stock_gap)
    categorized=_r(remaining-stock_gap)
    reusable=max(0.0,categorized) if roles and roles<={'purchase','regenerated'} else 0.0
    pending=max(0.0,categorized) if 'recovery' in roles else 0.0
    if not declarations:
        state,reason='conflict','barrel declaration missing for recorded solvent movement'
    elif out_l>in_l+TOL:
        state,reason='conflict','outflow exceeds inflow: same reflux cannot be both regenerated feedstock and outbound waste'
    elif not conversion_ok:
        state,reason='conflict','purchase weight and volume conversion basis inconsistent'
    elif surplus:
        state,reason='conflict','physical stock exceeds book balance'
    elif unexplained>TOL:
        state,reason='short','unexplained solvent loss requires investigation'
    elif pending>TOL or in_transit>TOL or unapproved:
        bits=[]
        if pending>TOL:bits.append('recovery stock awaits approved disposal')
        if in_transit>TOL:bits.append('handover not yet confirmed by weighed receiver receipt')
        if unapproved:bits.append('physical event lacks approved route')
        state,reason='open','; '.join(bits)
    else:
        state,reason='closed','mass balance closed with physical evidence'
    quantities={'purchased_l':purchased,'reflux_in_l':reflux_in,'regenerated_in_l':regenerated,'in_l':in_l,
                'drawn_l':drawn,'distilled_l':distilled,'handed_over_l':handed,'out_l':out_l,
                'remaining_l':remaining,'reusable_l':reusable,'pending_l':pending,
                'evaporation_l':evaporation.get(barrel_id,0.0),'residue_l':residue,
                'in_transit_l':in_transit,'transferred_l':transferred,'transfer_loss_l':transfer_loss,
                'still_loss_l':still_loss,'stock_gap_l':stock_gap,'unexplained_l':unexplained}
    return update(db,'solvent_decision',barrel_id,key='barrel_id',state=state,reason=reason,quantities=json.dumps(quantities,sort_keys=True))


def _decide_operations(db,ops):
    results=[]
    op_ids=set(ops)|{row['event_id'] for row in db.execute("SELECT event_id FROM operation WHERE kind='clean'")}
    for op_id in sorted(op_ids):
        flow=ops.get(op_id,{'drawn':0.0,'refluxed':0.0,'by_barrel':{}})
        drawn,refluxed=flow['drawn'],flow['refluxed']
        evaporation=_r(drawn-refluxed)
        op=db.execute('SELECT * FROM operation WHERE event_id=?',(op_id,)).fetchone()
        if op is None:
            state,reason='unpaired','solvent movement references unrecorded operation'
        elif drawn<TOL and refluxed<TOL:
            state,reason='unpaired','clean operation has no solvent draw'
        elif evaporation<-TOL:
            state,reason='conflict','reflux exceeds drawn solvent'
        else:
            shops={row['shop'] for row in db.execute('SELECT shop FROM solvent_draw WHERE operation_id=?',(op_id,))}
            garment=db.execute('SELECT * FROM garment WHERE id=?',(op['garment_id'],)).fetchone()
            intake=db.execute('SELECT * FROM intake_decision WHERE garment_id=?',(op['garment_id'],)).fetchone()
            recipe=db.execute('SELECT * FROM recipe WHERE event_id=?',(op['recipe_id'],)).fetchone()
            authorized=bool(garment and intake and intake['state']=='accepted' and recipe and garment['allowed_process'] and garment['allowed_process']==recipe['process'])
            if shops and shops!={op['from_shop']}:
                state,reason='conflict','draw shop does not match operation shop'
            elif not authorized:
                state,reason='unauthorized','operation lacks legal dry-clean authorization; solvent consumption retained in environmental ledger'
            else:
                state,reason='balanced','draw, reflux and evaporation conserved under legal authorization'
        quantities={'garment_id':op['garment_id'] if op else None,'drawn_l':drawn,'refluxed_l':refluxed,'evaporation_l':max(0.0,evaporation)}
        results.append(update(db,'solvent_operation',op_id,key='operation_id',state=state,reason=reason,quantities=json.dumps(quantities,sort_keys=True)))
    return results


def reconcile(db):
    ops=_operation_flows(db)
    evaporation=_barrel_evaporation(ops)
    barrels=[_decide_barrel(db,barrel_id,evaporation) for barrel_id in sorted(_barrel_ids(db))]
    return {'barrels':barrels,'operations':_decide_operations(db,ops)}


def import_data(db,data):
    with db:
        for row in data.get('routes',[]):
            if record(db,row['event_id'],'solvent_route',row):
                db.execute('INSERT INTO solvent_route VALUES(?,?,?,?,?,?)',
                           (row['event_id'],row['route_id'],row['kind'],row['approved_by'],row['valid_from'],row['valid_to']))
        for row in data.get('barrels',[]):
            if record(db,row['event_id'],'solvent_barrel',row):
                old=db.execute('SELECT * FROM solvent_barrel WHERE barrel_id=?',(row['barrel_id'],)).fetchone()
                if old:
                    if any(old[name]!=row[name] for name in ('batch','chemical','role','liters','weight_kg','density_kg_per_l','meter_id','at','supplier')):
                        raise ValueError(f"barrel conflict: {row['barrel_id']}")
                else:
                    db.execute('INSERT INTO solvent_barrel VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (row['event_id'],row['barrel_id'],row['batch'],row['chemical'],row['role'],row['liters'],row['weight_kg'],row['density_kg_per_l'],row['meter_id'],row['at'],row['supplier']))
        for row in data.get('draws',[]):
            if record(db,row['event_id'],'solvent_draw',row):
                db.execute('INSERT INTO solvent_draw VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'],row['barrel_id'],row['operation_id'],row['liters'],row['meter_id'],row['at'],row['shop']))
        for row in data.get('refluxes',[]):
            if record(db,row['event_id'],'solvent_reflux',row):
                db.execute('INSERT INTO solvent_reflux VALUES(?,?,?,?,?,?)',
                           (row['event_id'],row['barrel_id'],row['operation_id'],row['liters'],row['meter_id'],row['at']))
        for row in data.get('distillations',[]):
            if record(db,row['event_id'],'solvent_distillation',row):
                db.execute('INSERT INTO solvent_distillation VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['route_id'],row['input_barrel_id'],row['output_barrel_id'],row['input_liters'],row['reusable_liters'],row['residue_liters'],row['meter_id'],row['at']))
        for row in data.get('handovers',[]):
            if record(db,row['event_id'],'solvent_handover',row):
                db.execute('INSERT INTO solvent_handover VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['barrel_id'],row['route_id'],row['liters'],row['sealed_weight_kg'],row['carrier'],row['receiver'],row['meter_id'],row['at']))
        for row in data.get('waste_receipts',[]):
            if record(db,row['event_id'],'solvent_waste_receipt',row):
                db.execute('INSERT INTO solvent_waste_receipt VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'],row['handover_id'],row['status'],row.get('received_liters'),row.get('received_weight_kg'),row['at'],row['signer']))
        for row in data.get('calibrations',[]):
            if record(db,row['event_id'],'solvent_calibration',row):
                db.execute('INSERT INTO solvent_calibration VALUES(?,?,?,?,?)',
                           (row['event_id'],row['meter_id'],row['factor'],row['effective_from'],row['issued_at']))
        for row in data.get('stock_checks',[]):
            if record(db,row['event_id'],'solvent_stock_check',row):
                db.execute('INSERT INTO solvent_stock_check VALUES(?,?,?,?,?)',
                           (row['event_id'],row['barrel_id'],row['measured_liters'],row['meter_id'],row['at']))
        return reconcile(db)
