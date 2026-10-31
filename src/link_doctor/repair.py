"""Bounded, deterministic extensions of the existing archive-span search.

No semantic pruning certificate or impossibility claim: order permutations,
then demand-directed repeats (at most four), then contiguous subspan groups.
All edits stay inside a contiguous user archive span, never across a flag,
object, existing group or implicit runtime boundary.
"""
import itertools


def spans_of(nodes):
    spans, span = [], []
    for n in nodes:
        if n.position >= 0 and n.kind == 'archive' and not n.runtime:
            if span and n.position != span[-1].position + 1:
                spans.append(span); span = []
            span.append(n)
        elif span:
            spans.append(span); span = []
    if span: spans.append(span)
    return spans


def apply(nodes, mapping):
    return [v for n in nodes for v in mapping.get(id(n), [n])]


def command(argv, nodes, mapping, render):
    edits = {n.position: render(mapping[id(n)]) for n in nodes if id(n) in mapping}
    return [v for i,t in enumerate(argv) for v in edits.get(i,[t])]


def search(nodes, model, limit, group_factory):
    spans = spans_of(nodes)
    attempts = 0
    originals = []
    def permutations(k, mapping):
        if k == len(spans):
            yield mapping; return
        for order in itertools.permutations(spans[k]):
            yield from permutations(k+1, {**mapping, **{id(a):[b] for a,b in zip(spans[k],order)}})
    def valid(r): return not r['unresolved'] and not r['duplicates']
    for mapping in itertools.islice(permutations(0, {}), limit):
        attempts += 1
        r = model(apply(nodes, mapping))
        if valid(r): return mapping, r, {'kind':'reorder'}, attempts
        if not originals: originals.append((mapping, r))
    # Demand-directed breadth-first extension of the original order. Preserve
    # all occurrences, including ones that were not extracted. Never delete a
    # losing provider to make a command pass.
    frontier = originals[:1]
    for depth in range(1, 5):
        following = []
        for mapping, result in frontier:
            for span in spans:
                members = {m.identity for n in span for m in n.members}
                needed = {s for s in result['unresolved']
                          if any(c in members for c in result['consumers'][s])}
                for n in span:
                    if not any(m.defines.intersection(needed) for m in n.members): continue
                    if attempts >= limit: return None, None, None, attempts
                    end = span[-1]
                    updated = {**mapping, id(end):[*mapping.get(id(end),[end]), n]}
                    attempts += 1
                    r = model(apply(nodes, updated))
                    if valid(r): return updated, r, {'kind':'repeat','added_occurrences':depth}, attempts
                    # A repeat extracting nothing cannot create future demand.
                    if r['extracted'] != result['extracted']:
                        following.append((updated, r))
        frontier = following
        if not frontier: break
    # Smallest contiguous subspan first; leave unrelated archives outside.
    # Groups retain every occurrence and are never placed across barriers.
    for width in range(2, max((len(s) for s in spans), default=0) + 1):
        for span in spans:
            for start in range(len(span) - width + 1):
                if attempts >= limit: return None, None, None, attempts
                target = span[start:start + width]
                mapping = {id(n): [] for n in target}
                mapping[id(target[0])] = [group_factory(target)]
                attempts += 1
                r = model(apply(nodes, mapping))
                if valid(r):
                    return mapping, r, {'kind':'group', 'archives':[n.token for n in target]}, attempts
    return None, None, None, attempts


def provider_changes(original, candidate):
    old, new = original['selected_providers'], candidate['selected_providers']
    return [{'symbol':s,'from':old[s],'to':new[s]} for s in sorted(old.keys() & new.keys()) if old[s] != new[s]]
