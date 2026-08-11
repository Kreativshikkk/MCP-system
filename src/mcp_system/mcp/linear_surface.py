from __future__ import annotations
from typing import Any,Mapping
from .dispatcher import SurfaceSpec,ToolSpec
S={"type":"string","minLength":1}; I={"type":"integer","minimum":0}; SS={"type":"array","items":S}
def linear_graphql_surface()->SurfaceSpec:
    specs=(
      ("get_viewer",{},(),True),("list_users",{},(),True),("list_teams",{},(),True),
      ("list_workflow_states",{"teamId":S},("teamId",),True),("list_issue_labels",{"teamId":S},("teamId",),True),
      ("list_projects",{"teamId":S},(),True),("get_project",{"id":S},("id",),True),
      ("create_project",{"teamId":S,"name":S,"description":S,"leadId":S},("teamId","name"),False),("update_project",{"id":S,"name":S,"description":S,"state":S},("id",),False),
      ("list_cycles",{"teamId":S},("teamId",),True),("create_cycle",{"teamId":S,"name":S,"description":S,"startsAt":S,"endsAt":S},("teamId","name"),False),("update_cycle",{"id":S,"name":S,"description":S,"completedAt":S},("id",),False),
      ("list_issues",{"teamId":S,"stateId":S,"assigneeId":S},("teamId",),True),("get_issue",{"id":S},("id",),True),
      ("create_issue",{"teamId":S,"title":S,"description":S,"priority":I,"stateId":S,"assigneeId":S,"projectId":S,"cycleId":S,"labelIds":SS},("teamId","title"),False),
      ("update_issue",{"id":S,"title":S,"description":S,"priority":I,"stateId":S,"assigneeId":S,"projectId":S,"cycleId":S},("id",),False),("archive_issue",{"id":S},("id",),False),
      ("list_comments",{"issueId":S},("issueId",),True),("create_comment",{"issueId":S,"body":S},("issueId","body"),False),("create_issue_relation",{"issueId":S,"relatedIssueId":S,"type":S},("issueId","relatedIssueId","type"),False),
    ); return SurfaceSpec("linear_graphql","linear",tuple(_tool(*x) for x in specs))
def _tool(op:str,props:Mapping[str,Any],required:tuple[str,...],read:bool)->ToolSpec:
    schema={"type":"object","properties":dict(props),"additionalProperties":False};
    if required:schema["required"]=list(required)
    renames={k:_snake(k) for k in props if _snake(k)!=k}
    return ToolSpec(name=f"linear_{op}",title=op.replace("_"," ").title(),description=f"Linear GraphQL API: {op.replace('_',' ')}.",input_schema=schema,operation=op,argument_renames=renames,read_only=read,idempotent=read)
def _snake(value:str)->str:
    return "".join(("_"+c.lower()) if c.isupper() else c for c in value)
