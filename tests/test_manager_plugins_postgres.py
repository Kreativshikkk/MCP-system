from __future__ import annotations
import os,tempfile,unittest
from pathlib import Path
from uuid import uuid4
import psycopg
from psycopg import sql
from mcp_system import MCPSystem,PluginRegistry
from mcp_system.config import load_template_spec
from mcp_system.mcp import MCPDispatcher
from mcp_system.service_plugins import LinearPlugin,YouTrackPlugin
from mcp_system.storage import PostgresControlPlane,PostgresServiceStorage
@unittest.skipUnless(os.getenv("MCP_SYSTEM_TEST_POSTGRES_DSN"),"set MCP_SYSTEM_TEST_POSTGRES_DSN")
class ManagerPluginsPostgresTests(unittest.TestCase):
 def test_linear_and_youtrack_materialize_and_mutate(self):
  dsn=os.environ["MCP_SYSTEM_TEST_POSTGRES_DSN"];suffix=uuid4().hex[:10];control=f"manager_control_{suffix}";namespace=f"manager_state_{suffix}"
  try:
   with tempfile.TemporaryDirectory() as root:
    r=PluginRegistry();r.register(LinearPlugin());r.register(YouTrackPlugin());s=MCPSystem(Path(root),r,control_plane=PostgresControlPlane(dsn,schema=control),service_storage=PostgresServiceStorage(dsn,namespace=namespace))
    for file,tool,args in (("linear-default.toml","linear_create_issue",{"teamId":"team-1","title":"Postgres Linear"}),("youtrack-default.toml","youtrack_create_issue",{"projectId":"PROD","summary":"Postgres YouTrack"})):
     spec=load_template_spec(Path("configs/templates")/file);s.create_template(spec);e=s.create_environment_from_template(spec.template_id);result=MCPDispatcher(s,e.id,actor="lead").call_tool(tool,args);self.assertFalse(result["isError"],result["structuredContent"])
  finally:
   with psycopg.connect(dsn) as c:
    for (name,) in c.execute("SELECT nspname FROM pg_namespace WHERE nspname=%s OR nspname LIKE %s",(control,f"{namespace}\\_%")).fetchall():c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
