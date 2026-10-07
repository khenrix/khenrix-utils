"""Pure consumption of caller-supplied curated publications, never source bodies.

The caller owns canonical storage reads, completeness and retirement decisions.
This helper neither discovers files nor fetches citations. A follow result is
available curated evidence, not a claim that an upstream source was expanded.
"""
import copy
import re
from urllib.parse import quote

import curation
import sources


MAX_PUBLICATIONS = 8192
MAX_COLLECTION_BYTES = 64 * 1024 * 1024


def _reference(value):
    if type(value) is dict and value.get('schema_version') == sources.SOURCE_SCHEMA:
        return sources.validate_reference(value)
    curation._object(value, {'repository', 'commit', 'path'})
    repository = curation.validate_repository(value['repository'])
    if type(value['commit']) is not str or not re.fullmatch(r'[0-9a-f]{40}', value['commit']):
        raise ValueError('invalid pinned commit')
    return {'repository': repository, 'commit': value['commit'], 'path': curation._path(value['path'])}


def _key(reference):
    return curation._json(reference)


def _citation(reference):
    if 'schema_version' in reference:
        return reference['citation']
    return (f"https://github.com/{reference['repository']}/blob/{reference['commit']}/"
            + quote(reference['path'], safe='/'))


class CuratedReader:
    """Bounded in-memory views over existing v1/v2 publication checkpoints.

    Inputs are complete checkpoints, not filenames, raw notes or a new storage
    envelope. Retired references are explicit caller assertions; source prose,
    supersession and withdrawal language do not change lifecycle state here.
    """

    def __init__(self, publications, *, retired=None):
        if type(publications) is not list or len(publications) > MAX_PUBLICATIONS:
            raise ValueError('curated collection limit exceeded')
        self._rows = {}
        collection_bytes = 0
        revision_hashes = {}
        for publication in publications:
            curation._object(publication, {'schema_version', 'batch', 'published_at'})
            clean = curation._checkpoint(publication['batch'], publication['published_at'])
            curation._literal(publication['schema_version'], clean['schema_version'])
            collection_bytes += len(curation._json(clean).encode('utf-8'))
            if collection_bytes > MAX_COLLECTION_BYTES:
                raise ValueError('curated collection limit exceeded')
            batch = clean['batch']
            for card in batch['cards']:
                reference = (card['source'] if batch['schema_version'] == curation.SOURCE_BATCH_SCHEMA
                             else {'repository': batch['repository'], 'commit': batch['commit'], 'path': card['path']})
                if 'source' in card:
                    for ref in [card['source'], *card['dependencies'], *card['supersedes']]:
                        if ref['revision'] is not None:
                            identity = (sources.source_key(ref), ref['revision'])
                            if identity in revision_hashes and revision_hashes[identity] != ref['content_hash']:
                                raise ValueError('conflicting source revision')
                            revision_hashes[identity] = ref['content_hash']
                key = _key(reference)
                if key in self._rows:
                    raise ValueError('duplicate curated reference')
                self._rows[key] = {'reference': reference, 'citation': _citation(reference),
                                   'published_at': clean['published_at'], 'card': card}
        retired = [] if retired is None else retired
        if type(retired) is not list or len(retired) > MAX_PUBLICATIONS * curation.MAX_CARDS:
            raise ValueError('invalid retired references')
        for reference in retired:
            self._rows.pop(_key(_reference(reference)), None)

    def rows(self):
        """Copy the complete active collection for explicit topic/identity selection."""
        return copy.deepcopy(list(self._rows.values()))

    def search(self, query, *, limit=5):
        """Return at most 1..5 matches; all query words must occur in curated text."""
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError('invalid query limit')
        query = curation._text(query, 512)
        terms = re.findall(r'\w+', query.casefold())
        if not 1 <= len(terms) <= 16:
            raise ValueError('invalid query terms')
        matches = []
        for row in self._rows.values():
            card = row['card']
            words = set(re.findall(r'\w+', '\n'.join(
                [card['title'], card['summary'], card['details'], *card['uncertainty']]).casefold()))
            if all(term in words for term in terms):
                matches.append(copy.deepcopy(row))
                if len(matches) == limit:
                    break
        return matches

    def get(self, reference):
        """Get one exact actual source revision; missing or retired returns None."""
        return copy.deepcopy(self._rows.get(_key(_reference(reference))))

    def follow(self, origin, reference):
        """Follow one origin-bound citation within supplied curated evidence only."""
        origin = self.get(origin)
        reference = _reference(reference)
        if origin is None:
            raise ValueError('missing curated origin')
        card = origin['card']
        cited = list(card['source_links'])
        if 'source' in card:
            bound = [*card['dependencies'], *card['supersedes']]
            permitted = reference in bound or _citation(reference) in cited
        else:
            bound = [{**origin['reference'], 'path': path} for path in card['cross_references']]
            permitted = reference in bound or _citation(reference) in cited
        if not permitted:
            raise ValueError('reference is not cited by curated origin')
        return self.get(reference)

    def injection(self, query, *, token_counter=None):
        """Serialize at most two complete summaries within 500 aggregate tokens.

        The supplied counter must support the consumer's tokenizer. Its count is
        over the exact returned JSON, including provenance and uncertainty. No
        fallback estimate, summary rewriting or metadata stripping is performed.
        """
        candidates = self.search(query)
        if token_counter is None:
            return ''
        if not callable(token_counter):
            raise ValueError('unsupported token counter')
        selected = []
        result = ''
        for row in candidates:
            row['card'].pop('details')
            serialized = curation._json([*selected, row])
            count = token_counter(serialized)
            if type(count) is not int or count < 0:
                raise ValueError('invalid token count')
            if count <= 500:
                selected.append(row)
                result = serialized
                if len(selected) == 2:
                    break
        return result
