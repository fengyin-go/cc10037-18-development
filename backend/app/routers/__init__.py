"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

注意：通用资源路由里有 `/api/{module}/{entry_id}` 动态段，固定路径的
`/api/cost/...` 必须注册在它前面，否则会被当成 entry_id 解析。
"""
from __future__ import annotations

from app.routers import business, cost, resource

ROUTERS = [cost, resource, business]
