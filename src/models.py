from typing import List, Dict, Any, Optional

from pydantic import BaseModel


class BaseClusterSecret(BaseModel):
    uid: str
    name: str
    data: Dict[str, Any]
    metadata: Dict[str, Any]
    synced_namespace: List[str]
    type: str = "Opaque"
    match_namespace: Optional[List[str]] = None
    avoid_namespaces: Optional[List[str]] = None
