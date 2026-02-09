from typing import List, Dict, Any, Optional

from pydantic import BaseModel, Field, ConfigDict


class BaseClusterSecret(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    uid: str
    name: str
    data: Dict[str, Any]
    metadata: Dict[str, Any]
    synced_namespace: List[str]
    type: str = "Opaque"
    match_namespace: Optional[List[str]] = Field(None, alias="matchNamespace")
    avoid_namespaces: Optional[List[str]] = Field(None, alias="avoidNamespaces")

    @property
    def kubernetes_body(self) -> Dict[str, Any]:
        """Returns a dictionary formatted for sync_secret and Kubernetes API"""
        return {
            'metadata': self.metadata,
            'data': self.data,
            'type': self.type,
            'matchNamespace': self.match_namespace,
            'avoidNamespaces': self.avoid_namespaces,
        }
