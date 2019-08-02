from __future__ import annotations

import datetime
from dataclasses import dataclass
from itertools import chain
from typing import (
    NewType,
    Tuple,
    Union,
    List,
    Dict,
    Any,
    Set
)

from bson import ObjectId
from networkx import DiGraph

from matchengine.utilities.object_comparison import nested_object_hash

Trial = NewType("Trial", dict)
ParentPath = NewType("ParentPath", Tuple[Union[str, int]])
MatchClause = NewType("MatchClause", List[Dict[str, Any]])
MatchTree = NewType("MatchTree", DiGraph)
MultiCollectionQuery = NewType("MultiCollectionQuery", dict)
NodeID = NewType("NodeID", int)
MatchClauseLevel = NewType("MatchClauseLevel", str)
MongoQueryResult = NewType("MongoQueryResult", Dict[str, Any])
MongoQuery = NewType("MongoQuery", Dict[str, Any])
GenomicID = NewType("GenomicID", ObjectId)
ClinicalID = NewType("ClinicalID", ObjectId)
Collection = NewType("Collection", str)


class PoisonPill(object):
    pass


class CheckIndicesTask(object):
    pass


@dataclass
class IndexUpdateTask(object):
    collection: str
    index: str


@dataclass
class QueryTask:
    trial: Trial
    match_clause_data: MatchClauseData
    match_path: MatchCriterion
    query: MultiCollectionQuery
    clinical_ids: Set[ClinicalID]


@dataclass
class UpdateTask:
    ops: List
    protocol_no: str


@dataclass
class RunLogUpdateTask:
    protocol_no: str


Task = NewType("Task", Union[PoisonPill, CheckIndicesTask, IndexUpdateTask, QueryTask, UpdateTask])


@dataclass
class MatchCriteria:
    criteria: Dict
    depth: int


@dataclass
class MatchCriterion:
    criteria_list: List[MatchCriteria]

    def add_criteria(self, criteria: MatchCriteria):
        if hasattr(self, '_hash'):
            delattr(self, '_hash')
        self.criteria_list.append(criteria)

    def hash(self) -> str:
        if not hasattr(self, '_hash'):
            setattr(self,
                    '_hash',
                    nested_object_hash({"query": [criteria.criteria for criteria in self.criteria_list]}))
        return getattr(self, '_hash')


@dataclass
class QueryPart:
    query: Dict
    negate: bool
    render: bool
    mcq_invalidating: bool

    def hash(self) -> str:
        if not hasattr(self, '_hash'):
            setattr(self, '_hash', nested_object_hash(self.query))
        return getattr(self, '_hash')

    def __copy__(self):
        return QueryPart(self.query,
                         self.negate,
                         self.render,
                         self.mcq_invalidating)


class QueryNode:
    query_level: str
    query_depth: int
    query_parts: List[QueryPart]
    exclusion: Union[None, bool]
    is_finalized: bool

    def __init__(self, query_level, query_depth, query_parts, exclusion=None, is_finalized=False):

        self.is_finalized = is_finalized
        self.query_level = query_level
        self.query_depth = query_depth
        self.query_parts = query_parts
        self.exclusion = exclusion

    def hash(self) -> str:
        if not hasattr(self, '_hash'):
            setattr(self, '_hash', nested_object_hash({
                "_tmp1": [query_part.hash()
                          for query_part in self.query_parts],
                '_tmp2': self.exclusion
            }))
        return getattr(self, '_hash')

    def add_query_part(self, query_part: QueryPart):
        if hasattr(self, '_hash'):
            delattr(self, '_hash')
        self.query_parts.append(query_part)

    def extract_raw_query(self):
        raw_query = {
            key: value
            for query_part in self.query_parts
            for key, value in query_part.query.items()
            if query_part.render
        }
        if self.is_finalized:
            setattr(self, '_raw_query', raw_query)
        return raw_query

    def raw_query_hash(self):
        if not hasattr(self, '_raw_query_hash'):
            if not self.is_finalized:
                raise Exception("Query node is not finalized")
            else:
                setattr(self, '_raw_query_hash', nested_object_hash(self.extract_raw_query()))
        return getattr(self, '_raw_query_hash')

    def finalize(self):
        self.is_finalized = True

    def get_query_part_by_key(self, key: str) -> QueryPart:
        return next(chain((query_part
                           for query_part in self.query_parts
                           if key in query_part.query),
                          iter([None])))

    def get_query_part_value_by_key(self, key: str, default: Any = None) -> Any:
        query_part = self.get_query_part_by_key(key)
        if query_part is not None:
            return query_part.query.get(key, default)

    @property
    def mcq_invalidating(self):
        return True if any([query_part.mcq_invalidating for query_part in self.query_parts]) else False

    def __copy__(self):
        return QueryNode(self.query_level,
                         self.query_depth,
                         [query_part.__copy__()
                          for query_part
                          in self.query_parts],
                         self.exclusion,
                         self.is_finalized)


@dataclass
class MultiCollectionQuery:
    genomic: List[QueryNode]
    clinical: List[QueryNode]

    def __copy__(self):
        return MultiCollectionQuery(
            [query_node.__copy__()
             for query_node
             in self.genomic],
            [query_node.__copy__()
             for query_node
             in self.clinical],
        )

    @property
    def valid(self):
        return False if any([query_node.mcq_invalidating
                             for query_node
                             in chain(self.genomic, self.clinical)]) else True


@dataclass
class MatchClauseData:
    match_clause: MatchClause
    internal_id: str
    code: str
    coordinating_center: str
    is_suspended: bool
    status: str
    parent_path: ParentPath
    match_clause_level: MatchClauseLevel
    match_clause_additional_attributes: dict
    protocol_no: str


@dataclass
class GenomicMatchReason:
    query_node: QueryNode
    width: int
    clinical_id: ClinicalID
    genomic_id: Union[GenomicID, None]

    reason_name = 'genomic'


@dataclass
class ClinicalMatchReason:
    query_node: QueryNode
    clinical_id: ClinicalID
    reason_name = 'clinical'


MatchReason = NewType("MatchReason", Union[GenomicMatchReason, ClinicalMatchReason])


@dataclass
class TrialMatch:
    trial: Trial
    match_clause_data: MatchClauseData
    match_criterion: MatchCriterion
    multi_collection_query: MultiCollectionQuery
    match_reason: MatchReason
    run_log: datetime.datetime


class Cache(object):
    docs: Dict[ObjectId, MongoQueryResult]
    ids: dict
    run_log: dict

    def __init__(self):
        self.docs = dict()
        self.ids = dict()


@dataclass
class Secrets:
    HOST: str
    PORT: int
    DB: str
    AUTH_DB: str
    RO_USERNAME: str
    RO_PASSWORD: str
    RW_USERNAME: str
    RW_PASSWORD: str
    REPLICASET: str
    MAX_POOL_SIZE: str


class QueryTransformerResult:
    results: List[QueryPart]

    def __init__(
            self,
            query_clause: Dict = None,
            negate: bool = None,
            render: bool = True,
            mcq_invalidating: bool = False
    ):
        self.results = list()
        if query_clause is not None:
            if negate is not None:
                self.results.append(QueryPart(query_clause, negate, render, mcq_invalidating))
            else:
                raise Exception("If adding query result directly to results container, "
                                "both Negate and Query must be specified")

    def add_result(self, query_clause: Dict, negate: bool, render: bool = True, mcq_invalidating: bool = False):
        self.results.append(QueryPart(query_clause, negate, render, mcq_invalidating))
