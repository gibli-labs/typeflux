"""Self-contained human view; all repository-controlled strings become text nodes."""

from .repository import GraphError, canonical


def render(graph: dict, change: dict | None = None, verification: dict | None = None) -> str:
    if verification is not None and (
        verification.get("kind") != "ci-verification"
        or verification.get("testedRevision") != graph["revision"]
        or verification.get("treeDigest") != graph["treeDigest"]
        or not isinstance(verification.get("checks"), list)
    ):
        raise GraphError("CI evidence must match the graph's exact tested revision and inputs")
    payload = (
        canonical({"graph": graph, "impact": change, "verification": verification})
        .decode()
        .replace("<", "\\u003c")
    )
    return TEMPLATE.replace("__GRAPH_DATA__", payload)


TEMPLATE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Typeflux · Project graph</title>
<style>
:root{font:15px system-ui,sans-serif;color:#202331;background:#f6f7fb}*{box-sizing:border-box}body{margin:0}header{padding:28px 36px;background:#fff;border-bottom:1px solid #dce0ec}h1{margin:0 0 8px;font-size:28px}p{line-height:1.6}small,.muted{color:#5c6579}nav{display:flex;gap:8px;margin-top:20px;flex-wrap:wrap}button,input{font:inherit;padding:10px 14px;border:1px solid #cbd1e1;border-radius:8px;background:#fff}button{cursor:pointer;text-align:left}button:hover,button[aria-current=true]{border-color:#5145cc;background:#eeecff;color:#352c9d}input{width:100%;margin-bottom:14px}main{display:grid;grid-template-columns:330px 1fr;gap:24px;max-width:1400px;margin:28px auto;padding:0 28px}aside button{display:block;width:100%;margin-bottom:8px;overflow-wrap:anywhere}.card{background:white;padding:24px;border:1px solid #dce0ec;border-radius:12px;overflow-wrap:anywhere}h2{font-size:21px;margin-top:0}h3{font-size:16px;margin-top:24px}.badge{display:inline-block;padding:4px 8px;border-radius:5px;background:#eeecff;margin:0 6px 6px 0;font-size:12px}a{color:#4034b2}li{margin-bottom:10px;line-height:1.5}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f6f7fb;padding:14px}.step{border-left:3px solid #8a80e5;padding:4px 16px;margin-bottom:18px}footer{padding:20px 36px;color:#5c6579}@media(max-width:750px){main{grid-template-columns:1fr}header{padding:24px}aside{max-height:300px;overflow:auto}}
</style>
<header><h1>Typeflux project graph</h1><div id="revision"></div><p id="evidence" class="muted"></p><nav aria-label="Graph views">
<button data-view="capability">Capabilities</button><button data-view="component">Architecture</button><button data-view="journey">Journeys</button><button data-view="impact">Change impact</button>
</nav></header><main><aside><label for="search">Find an entity</label><input id="search" type="search" placeholder="Review, console, permissions…"><div id="catalog"></div></aside><section class="card" id="detail" aria-live="polite"></section></main>
<footer>Declared coverage ≠ passing verification ≠ observed behavior. Unknown mappings and missing evidence remain visible. This explorer cannot authorize rollout.</footer>
<script type="application/json" id="graph-data">__GRAPH_DATA__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('graph-data').textContent),graph=data.graph;
const nodes=new Map(graph.nodes.map(n=>[n.id,n])),catalog=document.getElementById('catalog'),detail=document.getElementById('detail');
let view='capability';
function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
document.getElementById('revision').textContent=`Revision ${graph.revision} · ${graph.dirty?'dirty workspace':'committed source'} · ${graph.nodes.length} entities · ${graph.unknownFiles.length} unmapped files`;
document.getElementById('evidence').textContent=data.verification?`Imported CI receipt · run ${data.verification.runId}, attempt ${data.verification.attempt} · ${data.verification.passed?'all required checks passed':'required checks not all passed'}. Identity matched to this snapshot; receipt authenticity is not verified offline. Observed product behavior: not recorded.`:'Verification: no execution receipt attached. Observed product behavior: not recorded.';
function sourceLink(node){const a=el('a',node.provenance.source);a.href='https://github.com/gibli-labs/typeflux/blob/'+encodeURIComponent(node.provenance.revision)+'/'+node.provenance.source.split('/').map(encodeURIComponent).join('/');a.rel='noopener noreferrer';a.target='_blank';return a;}
function show(id){const node=nodes.get(id);if(!node)return;detail.replaceChildren(el('h2',node.id),el('span',node.kind,'badge'),el('span',node.provenance.evidence,'badge'),el('p',node.description||node.path||node.name||''));detail.append(el('h3','Source'),sourceLink(node));
if(node.editions)detail.append(el('h3','Edition support'),el('pre',JSON.stringify(node.editions,null,2)));
if(node.steps){detail.append(el('h3','Journey and expected outcomes'));for(const step of node.steps){const block=el('div',undefined,'step');block.append(el('strong',step.id+' · '+step.action),el('p',step.outcome),el('small','Failure/recovery: '+step.failures.join('; ')),el('p','Declared checks: '+step.checks.join(', ')));detail.append(block);}}
detail.append(el('h3','Relationships'));const list=el('ul');for(const edge of graph.edges.filter(e=>e.source===id||e.target===id)){const other=edge.source===id?edge.target:edge.source;const li=el('li');li.append(el('span',edge.source===id?edge.relation+' → ':'← '+edge.relation+' '));const button=el('button',other);button.onclick=()=>show(other);li.append(button);list.append(li);}detail.append(list);
if(node.kind==='check'){const checks=data.verification?.checks||[];const outcomes=(node.ci||[]).map(name=>{const matches=checks.filter(c=>c.name===name);return matches.length===1?matches[0]:null;});const verified=outcomes.length>0&&outcomes.every(c=>c?.passed===true);detail.append(el('h3','Verification status'),el('p',verified?'Verified by the imported CI receipt: all mapped jobs passed. This does not establish observed product behavior.':'Unknown or not passing: no complete passing receipt for this check. The declaration alone is not execution evidence.'),el('pre',JSON.stringify({environment:node.environment,ci:node.ci,tests:node.tests,results:outcomes},null,2)));}}
function showImpact(){detail.replaceChildren(el('h2','Change impact'));if(!data.impact){detail.append(el('p','No change comparison attached. Build this explorer with --base to include a base/head impact report.'));return;}const r=data.impact;detail.append(el('p',r.mode),el('p',r.broadValidation?'Broad validation recommended: '+r.reasons.join('; '):'Graph recommendations are advisory; full CI remains mandatory.'),el('h3','Changed files'),el('pre',r.changedFiles.join('\n')||'No changes'),el('h3','Unmapped files'),el('pre',r.unknownFiles.join('\n')||'None in this change'),el('h3','Affected entities and explanation paths'));for(const item of r.affected){const details=el('details');details.append(el('summary',item.id),el('pre',JSON.stringify(item.via,null,2)));detail.append(details);}}
function refresh(){catalog.replaceChildren();if(view==='impact'){showImpact();return;}const query=document.getElementById('search').value.toLowerCase();const items=graph.nodes.filter(n=>n.kind===view&&[n.id,n.description].join(' ').toLowerCase().includes(query));for(const node of items){const button=el('button',node.id);button.onclick=()=>show(node.id);catalog.append(button);}if(items.length)show(items[0].id);else detail.replaceChildren(el('p','No matching entities.'));}
document.querySelectorAll('[data-view]').forEach(button=>{button.onclick=()=>{view=button.dataset.view;document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-current',String(b===button)));refresh();};});document.getElementById('search').oninput=refresh;document.querySelector('[data-view=capability]').setAttribute('aria-current','true');refresh();
</script></html>"""
