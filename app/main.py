"""Employee Attendance & Analytics API."""
import os, calendar
import json
from bson import json_util
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

def indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)], name="ix_attendance_status_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_emp_punch")
@app.on_event("startup")
def startup():
    indexes()

def err(code, msg): raise HTTPException(code, msg)
def parse_date(s):
    try: return date.fromisoformat(s)
    except Exception: err(422, "Invalid date; expected YYYY-MM-DD")
def parse_month(s):
    try:
        y,m=map(int,s.split("-"))
        if not 1<=m<=12: raise ValueError()
        return date(y,m,1), date(y,m,calendar.monthrange(y,m)[1])
    except Exception: err(422,"month must be YYYY-MM")
def dt_from_ms(v):
    if isinstance(v,bool) or not isinstance(v,(int,float)): err(422,"timestamp must be epoch milliseconds")
    try: return datetime.fromtimestamp(int(v)//1000, timezone.utc).replace(microsecond=0)
    except Exception: err(422,"invalid timestamp")
def now_utc(): return datetime.now(timezone.utc).replace(microsecond=0)
def as_utc(d):
    if d.tzinfo is None: return d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)
def ms(d): return int(as_utc(d).timestamp()*1000) if d else None
def local(d): return as_utc(d).astimezone(IST)
def clock(s):
    try: return time.fromisoformat(s)
    except Exception: err(422,"shift times must be HH:MM")
def shift_start_dt(emp, attendance_date):
    return datetime.combine(parse_date(attendance_date), clock(emp["shift_start"]), IST)
def shift_end_dt(emp, attendance_date):
    d=parse_date(attendance_date); start=clock(emp["shift_start"]); end=clock(emp["shift_end"])
    if end <= start: d += timedelta(days=1)
    return datetime.combine(d,end,IST)
def attendance_date_for(emp, instant):
    l=local(instant); d=l.date()
    # For overnight shift, early morning clock times belong to previous day's shift.
    if clock(emp["shift_end"]) <= clock(emp["shift_start"]) and l.time().replace(tzinfo=None) < clock(emp["shift_end"]):
        d -= timedelta(days=1)
    return d.isoformat()
def late_minutes(punch_in, emp, day):
    delta=(as_utc(punch_in)-shift_start_dt(emp,day).astimezone(timezone.utc)).total_seconds()
    return max(0, int(delta//60)) if delta > 600 else 0
def overtime_minutes(punch_out, emp, day):
    delta=(as_utc(punch_out)-shift_end_dt(emp,day).astimezone(timezone.utc)).total_seconds()
    mins=max(0,int(delta//60))
    return mins if mins>=30 else 0
def halfup(n, places=2):
    if n is None: return None
    q=Decimal("1").scaleb(-places)
    return float(Decimal(str(n)).quantize(q, rounding=ROUND_HALF_UP))
def calc_fields(doc, emp):
    pin=doc.get("punch_in"); pout=doc.get("punch_out"); status=doc.get("status")
    doc["late_minutes"]=late_minutes(pin,emp,doc["date"]) if pin and status in PRESENCE else 0
    if pin and pout and status in PRESENCE:
        hours=(as_utc(pout)-as_utc(pin)).total_seconds()/3600
        doc["work_hours"]=halfup(hours,2)
        doc["overtime_minutes"]=overtime_minutes(pout,emp,doc["date"])
        doc["half_day"]=doc["work_hours"] < 4.50
    else:
        doc["work_hours"]=None; doc["overtime_minutes"]=0; doc["half_day"]=False
    return doc
def public(doc):
    if not doc: return doc
    x=dict(doc); oid=x.pop("_id",None)
    if oid is not None: x["id"]=str(oid)
    for k in ("punch_in","punch_out","created_at"):
        if isinstance(x.get(k),datetime): x[k]=ms(x[k])
    for h in x.get("history",[]):
        if isinstance(h.get("at"),datetime): h["at"]=ms(h["at"])
        for change in h.get("changes",{}).values():
            for k in ("from","to"):
                if isinstance(change.get(k),datetime): change[k]=ms(change[k])
    return x
def valid_page(page,page_size):
    if page<1 or page_size<1 or page_size>100: err(422,"page must be >=1 and page_size must be 1..100")
def working_days(joined, start, end):
    j=parse_date(joined)
    a=max(start,j)
    if a>end:return 0
    return sum(1 for i in range((end-a).days+1) if (a+timedelta(days=i)).weekday()<5)
def mongo_date(d): return datetime.combine(d,time.min,timezone.utc)
def records_match(month):
    a,b=parse_month(month)
    return {"date":{"$gte":a.isoformat(),"$lte":b.isoformat()}}
def aggregate_round(field, places=2):
    factor=10**places
    return {"$divide":[{"$round":[{"$multiply":[field,factor]},0]},factor]}

class EmployeeIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1)
    email: str
    department: str = Field(min_length=1)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def check_joined(cls,v): parse_date(v); return v
    @field_validator("shift_start","shift_end")
    @classmethod
    def check_clock(cls,v): clock(v); return v
    def model_validate_extra(self):
        if self.shift_start == self.shift_end: err(422,"shift_start must differ from shift_end")

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT","WFH","ON_DUTY"] = "PRESENT"
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT","WFH","ON_DUTY","ABSENT","LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    by: str = Field(min_length=1)
    reason: str = Field(min_length=1)

@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status":"ok"}
    except Exception: err(503,"MongoDB unavailable")

@app.post("/employees",status_code=201)
def create_employee(body:EmployeeIn):
    if body.shift_start==body.shift_end: err(422,"shift_start must differ from shift_end")
    doc=body.model_dump(); doc["created_at"]=now_utc()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: err(409,"emp_code already exists")
    return public(doc)

@app.get("/employees")
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={"department":department} if department else {}
    total=db.employees.count_documents(q)
    docs=db.employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size)
    return {"items":list(docs),"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: err(404,"employee not found")
    ts=dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day=attendance_date_for(emp,ts)
    doc={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,
         "late_minutes":late_minutes(ts,emp,day),"overtime_minutes":0,"half_day":False,"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: err(409,"already punched in for this attendance date")
    return public(doc)

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: err(404,"employee not found")
    ts=dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts}},sort=[("punch_in",DESCENDING)])
    if not rec: err(404,"no punch-in found")
    if rec.get("punch_out") is not None: err(409,"record already punched out")
    

    punch_in = rec["punch_in"]

    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)

    if ts <= punch_in or ts > punch_in + timedelta(hours=24):
        err(422, "punch-out must be after punch-in and within 24 hours")

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts}},
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        err(409, "record already punched out")

    updated = calc_fields(updated, emp)
    
    db.attendance_logs.update_one(
        {"_id": updated["_id"]},
        {"$set": {
            k: updated[k]
            for k in ("late_minutes", "work_hours", "overtime_minutes", "half_day")
        }}
    )

    return public(updated)


@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,
                    page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if date_from: parse_date(date_from)
    if date_to: parse_date(date_to)
    if date_from and date_to and date_from>date_to: err(422,"date_from must be <= date_to")
    q={}
    if emp_code:q["emp_code"]=emp_code
    if status:
        if status not in STATUSES:err(422,"invalid status")
        q["status"]=status
    if date_from or date_to:
        q["date"]={}
        if date_from:q["date"]["$gte"]=date_from
        if date_to:q["date"]["$lte"]=date_to
    total=db.attendance_logs.count_documents(q)
    docs=db.attendance_logs.find(q).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items":[public(d) for d in docs],"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code:str,date:str,body:RegularizeIn):
    parse_date(date)
    if body.punch_in is None and body.punch_out is None and body.status is None: err(422,"no changes supplied")
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp:err(404,"employee not found")
    old=db.attendance_logs.find_one({"emp_code":emp_code,"date":date})
    if not old:err(404,"attendance record not found")
    new=dict(old); changes={}
    for field,val in (("status",body.status),("punch_in",dt_from_ms(body.punch_in) if body.punch_in is not None else None),
                      ("punch_out",dt_from_ms(body.punch_out) if body.punch_out is not None else None)):
        if val is not None and val!=old.get(field):
            changes[field]={"from":old.get(field),"to":val}; new[field]=val
    if not changes:err(422,"request does not change the record")
    if new["status"] in ("ABSENT","LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:err(422,"ABSENT/LEAVE cannot include punches")
        new["punch_in"]=new["punch_out"]=None
    elif new["status"] in PRESENCE:
        if not new.get("punch_in"):err(422,"presence status requires punch_in")
        if local(new["punch_in"]).date().isoformat()!=date:err(422,"punch_in must be on attendance date")
        if new.get("punch_out") and (new["punch_out"]<=new["punch_in"] or new["punch_out"]>new["punch_in"]+timedelta(hours=24)):err(422,"invalid punch_out")
    # Include implicit punch clearing in the audit trail.
    for f in ("punch_in","punch_out"):
        if old.get(f)!=new.get(f) and f not in changes:changes[f]={"from":old.get(f),"to":new.get(f)}
    new=calc_fields(new,emp)
    for f in ("late_minutes","work_hours","overtime_minutes","half_day"):
        if old.get(f)!=new.get(f):changes[f]={"from":old.get(f),"to":new.get(f)}
    entry={"at":now_utc(),"by":body.by,"reason":body.reason,"changes":changes}
    # optimistic concurrency: a concurrent update changes the history length / snapshot.
    result=db.attendance_logs.update_one({"_id":old["_id"],"history":old.get("history",[])},
        {"$set":{k:new[k] for k in ("status","punch_in","punch_out","late_minutes","work_hours","overtime_minutes","half_day")},
         "$push":{"history":entry}})
    if not result.modified_count:err(409,"record changed concurrently; retry")
    return public(db.attendance_logs.find_one({"_id":old["_id"]}))

def month_range(month):
    a,b=parse_month(month); return a.isoformat(),b.isoformat(),a,b
def rounded_pipeline(v,places=2):
    f=10**places
    return {"$divide":[{"$round":[{"$multiply":[v,f]},0]},f]}
def month_logs_pipeline(emp_code,month):
    a,b,_,_=month_range(month)
    return [{"$match":{"emp_code":emp_code,"date":{"$gte":a,"$lte":b}}},{"$group":{"_id":None,
      "present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$eq":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},1]}]},0,0]}},
      "leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},
      "late_count":{"$sum":{"$cond":[{"$gt":["$late_minutes",0]},1,0]}}}}]
def is_present_expr(): return {"$in":["$status",list(PRESENCE)]}
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str):
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp:err(404,"employee not found")
    a,b,da,dbb=month_range(month)
    # Aggregation computes attendance-record metrics in MongoDB; weekdays/working days use date arithmetic.
    pipe=[{"$match":{"emp_code":emp_code,"date":{"$gte":a,"$lte":b}}},
      {"$group":{"_id":None,"present_days":{"$sum":{"$cond":[{"$and":[is_present_expr(),{"$eq":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},1]}]},0,0]}},
      "leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"late_count":{"$sum":{"$cond":[{"$gt":["$late_minutes",0]},1,0]}}}}]
    # use aggregation for base counts; weekday present contribution corrected via daily date projection
    pipe=[{"$match":{"emp_code":emp_code,"date":{"$gte":a,"$lte":b}}},
          {"$addFields":{"_day":{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}}}},
          {"$group":{"_id":None,
            "present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$in":["$_day",[2,3,4,5,6]]}]},{"$cond":["$half_day",0.5,1]},0]}},
            "leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},
            "late_count":{"$sum":{"$cond":[{"$gt":["$late_minutes",0]},1,0]}}}}]
    agg=list(db.attendance_logs.aggregate(pipe))
    present=agg[0]["present_days"] if agg else 0
    joined=max(da,parse_date(emp["joined_on"]))
    working=working_days(emp["joined_on"],da,dbb)
    pct=halfup(present/working*100,4) if working else None
    return {"emp_code":emp_code,"month":month,"working_days":working,"present_days":present,"leave_days":agg[0]["leave_days"] if agg else 0,
            "late_count":agg[0]["late_count"] if agg else 0,"attendance_pct":pct}

@app.get("/analytics/departments/summary")
def department_summary(month:str,department:Optional[str]=None):
    a,b,da,dbb=month_range(month)
    match={"joined_on":{"$lte":dbb.isoformat()}}
    if department:match["department"]=department
    pipe=[{"$match":match},{"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code","joined":"$joined_on"},"pipeline":[
        {"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",a]},{"$lte":["$date",b]},{"$gte":["$date","$$joined"]}]}}},
        {"$addFields":{"_day":{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}}}},
        {"$group":{"_id":None,"present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$in":["$_day",[2,3,4,5,6]]}]},{"$cond":["$half_day",0.5,1]},0]}},
        "avg_work_hours":{"$avg":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$ne":["$work_hours",None]}]},"$work_hours",None]}},
        "late_count":{"$sum":{"$cond":[{"$gt":["$late_minutes",0]},1,0]}},"total_late_minutes":{"$sum":"$late_minutes"},
        "leave_count":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"on_duty_count":{"$sum":{"$cond":[{"$eq":["$status","ON_DUTY"]},1,0]}}}}],
        "as":"stats"}},{"$project":{"_id":0,"department":1,"headcount":{"$literal":1},"stats":{"$arrayElemAt":["$stats",0]}}},
        {"$group":{"_id":"$department","headcount":{"$sum":1},"present_days":{"$sum":{"$ifNull":["$stats.present_days",0]}},"avg_work_hours":{"$avg":"$stats.avg_work_hours"},
        "late_count":{"$sum":{"$ifNull":["$stats.late_count",0]}},"total_late_minutes":{"$sum":{"$ifNull":["$stats.total_late_minutes",0]}},"leave_count":{"$sum":{"$ifNull":["$stats.leave_count",0]}},"on_duty_count":{"$sum":{"$ifNull":["$stats.on_duty_count",0]}}}},
        {"$sort":{"_id":1}},{"$project":{"_id":0,"department":"$_id","headcount":1,"present_days":1,"avg_work_hours":rounded_pipeline("$avg_work_hours"),"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}}]
    return {"month":month,"items":list(db.employees.aggregate(pipe))}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    a,b,_,_=month_range(month)
    pipe=[{"$match":{"date":{"$gte":a,"$lte":b},"late_minutes":{"$gt":0}}},
          {"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"}}},
          {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"emp"}},
          {"$unwind":"$emp"}]
    if department:pipe.append({"$match":{"emp.department":department}})
    pipe += [{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},
             {"$match":{"rank":{"$lte":limit}}},{"$sort":{"total_late_minutes":-1,"_id":1}},
             {"$project":{"_id":0,"emp_code":"$_id","name":"$emp.name","department":"$emp.department","total_late_minutes":1,"rank":1}}]
    return {"month":month,"items":list(db.attendance_logs.aggregate(pipe))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_:str=Query(alias="from"),to:str=Query(...)):
    start=parse_date(from_); end=parse_date(to)
    if end<start or (end-start).days+1>92:err(422,"invalid date range (maximum 92 days)")
    if not db.employees.find_one({"department":department}):err(404,"department not found")
    # Aggregation densifies the calendar, including no-log days; moving average is calculated in MongoDB.
    start_s=start.isoformat(); end_s=end.isoformat()
    pipe=[{"$match":{"date":{"$gte":start_s,"$lte":end_s}}},
      {"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"e"}},{"$unwind":"$e"},
      {"$match":{"e.department":department}},
      {"$group":{"_id":"$date","present_count":{"$sum":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":["$half_day",0.5,1]},0]}},
       "late_count":{"$sum":{"$cond":[{"$gt":["$late_minutes",0]},1,0]}}}},
      {"$set":{"day":{"$dateFromString":{"dateString":"$_id"}}}},
      {"$densify":{"field":"day","range":{"step":1,"unit":"day","bounds":[datetime.combine(start,time.min),datetime.combine(end,time.min)]}}},
      {"$set":{"date":{"$dateToString":{"format":"%Y-%m-%d","date":"$day"}}}},
      {"$lookup":{"from":"employees","let":{"d":"$date"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$department",department]},{"$lte":["$joined_on","$$d"]}]}}},{"$count":"n"}],"as":"hc"}},
      {"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]},"present_count":{"$ifNull":["$present_count",0]},"late_count":{"$ifNull":["$late_count",0]},"is_working_day":{"$lte":[{"$dayOfWeek":"$day"},6]}}},
      {"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},rounded_pipeline({"$divide":["$present_count","$headcount"]},4),None]}}},
      {"$setWindowFields":{"sortBy":{"day":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
      {"$sort":{"day":1}},{"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":1}}]
    # Densify may return a datetime outside local range on some Mongo versions; bound results defensively.
    items=list(db.attendance_logs.aggregate(pipe))
    return {"department":department,"from":start_s,"to":end.isoformat(),"items":[x for x in items if start_s<=x["date"]<=end.isoformat()]}

def explain_find(collection, query, sort=None, skip=0, limit=20):
    find = collection.find(query)
    if sort:
        find = find.sort(sort)
    find = find.skip(skip).limit(limit)
    result = find.explain()
    return json.loads(json_util.dumps(result))

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),
                     date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,from_:Optional[str]=Query(None,alias="from"),to:Optional[str]=None,
                     page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if status:q["status"]=status
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=date_from
            if date_to:q["date"]["$lte"]=date_to
        ex=explain_find(db.attendance_logs,q,[("date",-1),("emp_code",1)],(page-1)*page_size,page_size)
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":ex}
    if endpoint=="employee_monthly":
        if not emp_code or not month:err(422,"emp_code and month are required")
        a,b,_,_=month_range(month)
        pipe=[{"$match":{"emp_code":emp_code,"date":{"$gte":a,"$lte":b}}},{"$group":{"_id":"$emp_code","records":{"$sum":1}}}]
        result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        result = json.loads(json_util.dumps(result))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}
    if endpoint=="department_summary":
        if not month:err(422,"month is required")
        a,b,_,_=month_range(month)
        pipe=[{"$match":{"date":{"$gte":a,"$lte":b}}},{"$group":{"_id":"$emp_code","n":{"$sum":1}}}]
        result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        result = json.loads(json_util.dumps(result))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}
    if endpoint=="late_leaderboard":
        if not month:err(422,"month is required")
        a,b,_,_=month_range(month); q={"date":{"$gte":a,"$lte":b},"late_minutes":{"$gt":0}}
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":explain_find(db.attendance_logs,q,[("late_minutes",-1)],0,limit)}
    if endpoint=="department_trend":
        if not department or not from_ or not to:err(422,"department, from and to are required")
        start=parse_date(from_); end=parse_date(to)
        if end<start or (end-start).days>91:err(422,"invalid date range")
        q={"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":explain_find(db.attendance_logs,q,[("date",1)],0,1000)}
    err(422,"unknown endpoint")

