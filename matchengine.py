from match_criteria_transform import MatchCriteriaTransform
from mongo_connection import MongoDBConnection
from collections import deque, defaultdict
from typing import Generator, Set
from frozendict import frozendict
from multiprocessing import cpu_count

import pymongo.database
import networkx as nx
import logging
import json
import argparse
import asyncio

from matchengine_types import *
from trial_match_utils import *

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('matchengine')


async def queue_worker(q, result_q, config, worker_id) -> None:
    if not q.empty():
        match_criteria_transform = MatchCriteriaTransform(config)
        with MongoDBConnection(read_only=True) as db:
            while not q.empty():
                task: QueueTask = await q.get()
                try:
                    # logging.info("Worker: {}, query: {}".format(worker_id, task.query))
                    log.info(
                        "Worker: {}, protocol_no: {} got new task".format(worker_id, task.trial['protocol_no']))
                    async for result in run_query(task.cache, db, match_criteria_transform, task.queries):
                        await result_q.put((task, result))
                        # log.info("Worker: {},  protocol_no: {}, clinical_id: {}, qsize: {}".format(worker_id,
                        #                                                                            task.trial[
                        #                                                                                'protocol_no'],
                        #                                                                            result.clinical_id,
                        #                                                                            q.qsize()))
                    q.task_done()
                except Exception as e:
                    log.error("ERROR: Worker: {}, error: {}".format(worker_id, e))
                    q.task_done()
                    await q.put(task)
                    raise e


async def find_matches(sample_ids: list = None,
                       protocol_nos: list = None,
                       debug: bool = False,
                       num_workers: int = 25,
                       match_on_closed: bool = False,
                       match_on_deceased: bool = False) -> Generator[TrialMatch,
                                                                     None,
                                                                     None]:
    """
    Take a list of sample ids and trial protocol numbers, return a dict of trial matches
    :param sample_ids:
    :param protocol_nos:
    :param debug:
    :param num_workers
    :return:
    """
    log.info('Beginning trial matching.')

    with open("config/config.json") as config_file_handle:
        config = json.load(config_file_handle)

    # init
    q = asyncio.queues.Queue()
    result_q = asyncio.queues.Queue()
    match_criteria_transform = MatchCriteriaTransform(config)

    cache = Cache(int(), int(), int(), int(), dict(), dict())

    with MongoDBConnection(read_only=True) as db:
        trials = [trial async for trial in get_trials(db, match_criteria_transform, protocol_nos, match_on_closed)]
        _ids = await get_clinical_ids_from_sample_ids(db, sample_ids, match_on_deceased)

    for trial in trials:
        log.info("Begin Protocol No: {}".format(trial["protocol_no"]))
        for match_clause in extract_match_clauses_from_trial(trial, match_on_closed):
            for match_path in get_match_paths(create_match_tree(match_clause.match_clause)):
                translated_match_path = translate_match_path(match_clause,
                                                             match_path,
                                                             match_criteria_transform)
                query = add_ids_to_query(translated_match_path, _ids, match_criteria_transform)
                if debug:
                    log.info("Query: {}".format(query))
                await q.put(QueueTask(match_criteria_transform,
                                      trial,
                                      match_clause,
                                      match_path,
                                      query,
                                      _ids,
                                      cache))
    workers = [asyncio.create_task(queue_worker(q, result_q, config, i))
               for i in range(0, min(q.qsize(), num_workers))]
    await asyncio.gather(*workers)
    await q.join()
    logging.info("Total results: {}".format(result_q.qsize()))
    logging.info("CLINICAL HITS: {}, CLINICAL MISSES: {}, GENOMIC HITS: {}, GENOMIC MISSES: {}".format(
        cache.clinical_hits,
        cache.clinical_non_hits,
        cache.genomic_hits,
        cache.genomic_non_hits
    ))
    while not result_q.empty():
        task: QueueTask
        result: RawQueryResult
        task, result = await result_q.get()
        yield TrialMatch(task.trial, task.match_clause_data, task.match_path, task.queries, result)


async def get_trials(db: pymongo.database.Database,
                     match_criteria_transform: MatchCriteriaTransform,
                     protocol_nos: list = None,
                     match_on_closed: bool = False) -> Generator[Trial, None, None]:
    trial_find_query = dict()

    # the minimum criteria needed in a trial projection. add extra values in config.json
    projection = {'protocol_no': 1, 'nct_id': 1, 'treatment_list': 1, 'status': 1}
    projection.update(match_criteria_transform.trial_projection)

    if protocol_nos is not None:
        trial_find_query['protocol_no'] = {"$in": [protocol_no for protocol_no in protocol_nos]}

    async for trial in db.trial.find(trial_find_query, projection):
        if trial['status'].lower().strip() not in {"open to accrual"} and not match_on_closed:
            logging.info('Trial %s is closed, skipping' % trial['protocol_no'])
        else:
            yield Trial(trial)


async def get_clinical_ids_from_sample_ids(db, sample_ids: List[str],
                                           match_on_deceased: bool = False) -> List[ClinicalID]:
    # if no sample ids are passed in as args, get all clinical documents
    if sample_ids is None:
        query = {} if match_on_deceased else {"VITAL_STATUS": 'alive'}
        return [result['_id']
                for result in await db.clinical.find(query, {"_id": 1}).to_list(None)]
    else:
        return [result['_id']
                for result in await db.clinical.find({"SAMPLE_ID": {"$in": sample_ids}}, {"_id": 1}).to_list(None)]


def extract_match_clauses_from_trial(trial: Trial,
                                     match_on_closed: bool = False) -> Generator[MatchClauseData, None, None]:
    """
    Pull out all of the matches from a trial curation.
    Return the parent path and the values of that match clause.

    Default to only extracting match clauses on steps, arms or dose levels which are open to accrual unless otherwise
    specified

    :param match_on_closed:
    :param trial:
    :return:
    """

    # find all match clauses. place everything else (nested dicts/lists) on a queue
    process_q = deque()
    for key, val in trial.items():

        # include top level match clauses
        if key == 'match':
            # TODO uncomment, for now don't match on top level match clauses
            continue
        #     parent_path = ParentPath(tuple())
        #     yield parent_path, val
        else:
            process_q.append((tuple(), key, val))

    # process nested dicts to find more match clauses
    while process_q:
        path, parent_key, parent_value = process_q.pop()
        if isinstance(parent_value, dict):
            for inner_key, inner_value in parent_value.items():
                if inner_key == 'match':
                    if path[-1] == 'arm':
                        if not match_on_closed and \
                                parent_value.setdefault('arm_suspended', 'n').lower().strip() == 'y':
                            continue
                    elif path[-1] == 'dose':
                        if not match_on_closed and \
                                parent_value.setdefault('level_suspended', 'n').lower().strip() == 'y':
                            continue
                    elif path[-1] == 'step':
                        if not match_on_closed and \
                                all([arm.setdefault('arm_suspended', 'n').lower().strip() == 'y'
                                     for arm in parent_value.setdefault('arm', list({'arm_suspended': 'y'}))]):
                            continue

                    parent_path = ParentPath(path + (parent_key, inner_key))
                    level = MatchClauseLevel([item for item in parent_path[::-1] if not isinstance(item, int)][0])

                    yield MatchClauseData(inner_value, parent_path, level, parent_value)
                else:
                    process_q.append((path + (parent_key,), inner_key, inner_value))
        elif isinstance(parent_value, list):
            for index, item in enumerate(parent_value):
                process_q.append((path + (parent_key,), index, item))


def create_match_tree(match_clause: MatchClause) -> MatchTree:
    process_q: deque[Tuple[NodeID, Dict[str, Any]]] = deque()
    graph = nx.DiGraph()
    node_id: NodeID = NodeID(1)
    graph.add_node(0)  # root node is 0
    graph.nodes[0]['criteria_list'] = list()
    for item in match_clause:
        process_q.append((NodeID(0), item))
    while process_q:
        parent_id, values = process_q.pop()
        parent_is_or = True if graph.nodes[parent_id].setdefault('is_or', False) else False
        for label, value in values.items():  # label is 'and', 'or', 'genomic' or 'clinical'
            if label == 'and':
                for item in value:
                    process_q.append((parent_id, item))
            elif label == "or":
                graph.add_edges_from([(parent_id, node_id)])
                graph.nodes[node_id]['criteria_list'] = list()
                graph.nodes[node_id]['is_or'] = True
                for item in value:
                    process_q.append((node_id, item))
                node_id += 1
            elif parent_is_or:
                graph.add_edges_from([(parent_id, node_id)])
                graph.nodes[node_id]['criteria_list'] = [values]
                node_id += 1
            else:
                graph.nodes[parent_id]['criteria_list'].append({label: value})
    return MatchTree(graph)


def get_match_paths(match_tree: MatchTree) -> Generator[MatchCriterion, None, None]:
    leaves = list()
    for node in match_tree.nodes:
        if match_tree.out_degree(node) == 0:
            leaves.append(node)
    for leaf in leaves:
        path = nx.shortest_path(match_tree, 0, leaf) if leaf != 0 else [leaf]
        match_path = MatchCriterion(list())
        for node in path:
            match_path.append(match_tree.nodes[node]['criteria_list'])
        yield match_path


def translate_match_path(match_clause_data: MatchClauseData,
                         match_criterion: MatchCriterion,
                         match_criteria_transformer: MatchCriteriaTransform) -> List[MultiCollectionQuery]:
    """
    Translate the keys/values from the trial curation into keys/values used in a genomic/clinical document.
    Uses an external config file ./config/config.json

    :param match_clause_data:
    :param match_criterion:
    :param match_criteria_transformer:
    :return:
    """
    output = list()
    for node in match_criterion:
        categories = MultiCollectionQuery(defaultdict(list))
        for criteria in node:
            for genomic_or_clinical, values in criteria.items():
                and_query = dict()
                for trial_key, trial_value in values.items():
                    trial_key_settings = match_criteria_transformer.trial_key_mappings[genomic_or_clinical].setdefault(
                        trial_key.upper(),
                        dict())

                    if 'ignore' in trial_key_settings and trial_key_settings['ignore']:
                        continue

                    sample_value_function_name = trial_key_settings.setdefault('sample_value', 'nomap')
                    sample_function = MatchCriteriaTransform.__dict__[sample_value_function_name]
                    args = dict(sample_key=trial_key.upper(),
                                trial_value=trial_value,
                                parent_path=match_clause_data.parent_path,
                                trial_path=genomic_or_clinical,
                                trial_key=trial_key)
                    args.update(trial_key_settings)
                    and_query.update(sample_function(match_criteria_transformer, **args))
                if and_query:
                    categories[genomic_or_clinical].append(and_query)
        if categories:
            output.append(categories)
    return output


def add_ids_to_query(multi_collection_queries: List[MultiCollectionQuery],
                     id_list: List[ClinicalID],
                     match_criteria_transformer: MatchCriteriaTransform) -> List[MultiCollectionQuery]:
    for query in multi_collection_queries:
        if id_list is not None:
            query[match_criteria_transformer.CLINICAL].append({
                match_criteria_transformer.primary_collection_unique_field: {"$in": id_list}
            })
            for genomic_query in query.setdefault('genomic', list()):
                genomic_query[match_criteria_transformer.collection_mappings['genomic']['join_field']] = {
                    "$in": id_list}
    return multi_collection_queries


async def execute_clinical_query(db: pymongo.database.Database,
                                 match_criteria_transformer: MatchCriteriaTransform,
                                 multi_collection_query: MultiCollectionQuery) -> Set[ObjectId]:
    if match_criteria_transformer.CLINICAL in multi_collection_query:
        collection = match_criteria_transformer.CLINICAL
        query = {"$and": multi_collection_query[collection]}
        cursor = await db[collection].find(query, {"_id": 1}).to_list(None)
        clinical_ids = {doc['_id'] for doc in cursor}
        return clinical_ids


async def run_query(cache: Cache,
                    db: pymongo.database.Database,
                    match_criteria_transformer: MatchCriteriaTransform,
                    multi_collection_queries: List[MultiCollectionQuery]) -> Generator[RawQueryResult,
                                                                                       None,
                                                                                       RawQueryResult]:
    """
    Execute a mongo query on the clinical and genomic collections to find trial matches.
    First execute the clinical query. If no records are returned short-circuit and return.

    :param db:
    :param match_criteria_transformer:
    :param multi_collection_query:
    :return:
    """
    # TODO refactor into smaller functions
    all_results: Dict[ObjectId, Set[ObjectId]] = defaultdict(set)

    clinical_ids = set()
    for multi_collection_query in multi_collection_queries:
        # get clinical docs first
        new_clinical_ids = await execute_clinical_query(db,
                                                        match_criteria_transformer,
                                                        multi_collection_query)

        # If no clinical docs are returned, skip executing genomic portion of the query
        if not new_clinical_ids:
            return
        for key in new_clinical_ids:
            clinical_ids.add(key)

        # iterate over all queries
        for items in multi_collection_query.items():
            genomic_or_clinical, queries = items

            # skip clinical queries as they've already been executed
            if genomic_or_clinical == match_criteria_transformer.CLINICAL and clinical_ids:
                continue

            join_field = match_criteria_transformer.collection_mappings[genomic_or_clinical]['join_field']

            for query in queries:
                # cache hit or miss should be here
                query.update({join_field: {"$in": list(clinical_ids)}})
                if join_field in query:
                    new_query = {"$and": list()}
                    for k, v in query.items():
                        if k == join_field:
                            new_query['$and'].insert(0, {k: v})
                        else:
                            new_query['$and'].append({k: v})
                else:
                    new_query = query
                clinical_result_ids = set()
                cursor = await db[genomic_or_clinical].find(new_query, {"_id": 1, "CLINICAL_ID": 1}).to_list(None)
                for result in cursor:
                    all_results[result[join_field]].add(result["_id"])
                    clinical_result_ids.add(result[join_field])

                clinical_ids.intersection_update(clinical_result_ids)

                if not clinical_ids:
                    return

        needed_clinical = list()
        needed_genomic = list()
        for clinical_id, genomic_ids in all_results.items():
            if clinical_id not in cache.docs:
                needed_clinical.append(clinical_id)
            for genomic_id in genomic_ids:
                if genomic_id not in cache.docs:
                    needed_genomic.append(genomic_id)

            # minimum fields required to execute matching. Extra matching fields can be added in config.json
        genomic_projection = {
            "SAMPLE_ID": 1,
            "CLINICAL_ID": 1,
            "VARIANT_CATEGORY": 1,
            "WILDTYPE": 1,
            "TIER": 1,
            "TRUE_HUGO_SYMBOL": 1,
            "TRUE_PROTEIN_CHANGE": 1,
            "CNV_CALL": 1,
            "TRUE_VARIANT_CLASSIFICATION": 1,
            "MMR_STATUS": 1
        }
        genomic_projection.update(match_criteria_transformer.genomic_projection)

        # minimum projection necessary for matching. Append extra values from config if desired
        clinical_projection = {
            "SAMPLE_ID": 1,
            "MRN": 1,
            "ONCOTREE_PRIMARY_DIAGNOSIS_NAME": 1,
            "VITAL_STATUS": 1,
            "FIRST_LAST": 1
        }
        clinical_projection.update(match_criteria_transformer.clinical_projection)

        async def perform_db_call(collection, query, projection):
            return await db[collection].find(query, projection).to_list(None)
        results = await asyncio.gather(perform_db_call("clinical",
                                                       {"_id": {"$in": list(needed_clinical)}},
                                                       clinical_projection),
                                       perform_db_call("genomic",
                                                       {"_id": {"$in": list(needed_genomic)}},
                                                       genomic_projection))
        for result in results:
            for doc in result:
                cache.docs[doc["_id"]] = doc
        for clinical_id, genomic_ids in all_results.items():
            yield RawQueryResult(multi_collection_query,
                                 ClinicalID(clinical_id),
                                 cache.docs[clinical_id],
                                 [cache.docs[genomic_id] for genomic_id in genomic_ids])


def create_trial_match(trial_match: TrialMatch) -> Dict:
    """
    Create a trial match document to be inserted into the db. Add clinical, genomic, and trial details as specified
    in config.json
    :param trial_match:
    :return:
    """
    # remove extra fields from trial_match output
    trial = dict()
    for key in trial_match.trial:
        if key in ['treatment_list', '_summary', 'status', '_id']:
            continue
        else:
            trial[key] = trial_match.trial[key]

    if trial_match.raw_query_result.genomic_docs:
        for genomic_doc in trial_match.raw_query_result.genomic_docs:
            for genomic_query in [{k: v for k, v in genomic_query.items() if k != "CLINICAL_ID"}
                                  for query in trial_match.multi_collection_queries if 'genomic' in query
                                  for genomic_query in query['genomic']]:
                new_trial_match = {
                    **format(trial_match.raw_query_result.clinical_doc),
                    **format(get_genomic_details(genomic_doc, genomic_query)),
                    **trial_match.match_clause_data.match_clause_additional_attributes,
                    **trial,
                    "query": trial_match.match_criterion
                }

                # add hash
                new_trial_match['hash'] = comparable_dict(new_trial_match).hash()
                yield new_trial_match
    else:
        new_trial_match = {
            **format(trial_match.raw_query_result.clinical_doc),
            **format(get_genomic_details(None, None)),
            **trial_match.match_clause_data.match_clause_additional_attributes,
            **trial,
            "query": trial_match.match_criterion
        }
        new_trial_match['hash'] = comparable_dict(new_trial_match).hash()
        yield new_trial_match


async def update_trial_matches(trial_matches: List[Dict], protocol_nos, sample_ids):
    """
    Update trial matches by diff'ing the newly created trial matches against existing matches in the db.
    'Delete' matches by adding {is_disabled: true} and insert all new matches.
    :param trial_matches:
    :param protocol_nos:
    :param sample_ids:
    :return:
    """
    new_matches_hashes = [match['hash'] for match in trial_matches]

    query = {'hash': {'$in': new_matches_hashes}}
    if protocol_nos is not None:
        query.update({'protocol_no': {'$in': protocol_nos}})

    if sample_ids is not None:
        query.update({'sample_id': {'$in': sample_ids}})

    with MongoDBConnection(read_only=True) as db:
        trial_matches_to_not_change = await db.trial_match_test.find(query, {"hash": 1}).to_list(None)
    del query['hash']

    where = {'hash': {'$nin': new_matches_hashes}}
    where.update(query)
    update = {"$set": {'is_disabled': True}}

    trial_matches_to_insert = [match for match in trial_matches if match['hash'] not in trial_matches_to_not_change]

    with MongoDBConnection(read_only=False) as db:
        async def delete():
            await db.trial_match_test.update_many(where, update)

        async def insert():
            await db.trial_match_test.insert_many(trial_matches_to_insert)

        await asyncio.gather(asyncio.create_task(delete()), asyncio.create_task(insert()))


async def check_indexes():
    """
    Ensure indexes exist on the trial_match collection so queries are performant
    :return:
    """
    with MongoDBConnection(read_only=True) as db:
        indexes = db.trial_match.list_indexes()
        existing_indexes = set()
        desired_indexes = {'hash', 'mrn', 'sample_id', 'clinical_id', 'protocol_no'}
        async for index in indexes:
            index_key = list(index['key'].to_dict().keys())[0]
            existing_indexes.add(index_key)
        indexes_to_create = desired_indexes - existing_indexes
        for index in indexes_to_create:
            log.info('Creating index %s' % index)
            await db.trial_match.create_index(index)


async def main(args):
    await check_indexes()
    trial_matches = find_matches(sample_ids=args.samples,
                                 protocol_nos=args.trials,
                                 num_workers=args.workers[0],
                                 match_on_closed=args.match_on_closed,
                                 match_on_deceased=args.match_on_deceased)
    all_new_matches = list()
    async for match in trial_matches:
        for inner_match in create_trial_match(match):
            all_new_matches.append(inner_match)

    if all_new_matches:
        await update_trial_matches(all_new_matches, args.trials, args.samples)


if __name__ == "__main__":
    # todo handle ! NOT criteria
    # todo run log
    # todo unit tests
    # todo refactor run_query
    # todo load functions
    # todo output CSV file functions

    parser = argparse.ArgumentParser()
    closed_help = 'Match on closed trials and all suspended steps, arms and doses.'
    deceased_help = 'Match on deceased patients.'
    parser.add_argument("-trials", nargs="*", type=str, default=None)
    parser.add_argument("-samples", nargs="*", type=str, default=None)
    parser.add_argument("--match-on-closed", dest="match_on_closed", action="store_true", default=False,
                        help=closed_help)
    parser.add_argument("--match-on-deceased-patients", dest="match_on_deceased", action="store_true",
                        help=deceased_help)
    parser.add_argument("-workers", nargs=1, type=int, default=[cpu_count() * 5])
    args = parser.parse_args()
    asyncio.run(main(args))
