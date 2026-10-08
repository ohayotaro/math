"""Rebuild a complete, explicitly non-proof-audited manuscript inventory."""

import argparse
import hashlib
import html
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parents[1] / "research"
SOURCE_COMMIT = "adc7f1241b42e322a6451854ab7e4b4c146bf78a"
TERMS = (
    "cryptograph", "blockchain", "consensus", "byzantine", "pseudorandom",
    "erasure", "codeword", "expander", "ramanujan", "switch chain",
    "scheduling", "thin tree", "sampling", "quantum factoring",
)
SELECTED = {
    "131": ("implemented-kernel", "Degree-preserving candidate generation; no uniformity claim."),
    "178": ("implemented-verifier", "Exact finite spectral admission test, not full construction."),
}
CANDIDATES = {
    "089": "Flow/cut bounds require planar or bounded-treewidth hosts; not our regular overlay.",
    "097": "Vector balancing could inform execution scheduling; no implemented efficient general scheduler.",
    "099": "Edit-distance embeddings are not erasure coding or authenticated state synchronization.",
    "102": "Approximation hardness warns against promising universally optimal resource allocation.",
    "103": "Logspace derandomization is not a cryptographic PRG or unpredictable leader beacon.",
    "104": "Game algorithms might model incentives; no economic model is specified or validated.",
    "107": "Asymptotic matrix multiplication is not a measured finite-size cryptographic speedup.",
    "109": "Exponent saving 2^-182 is asymptotic; not a practical arithmetic optimization here.",
    "110": "k-server may inform caches; metric movement and oblivious requests differ from Byzantine traffic.",
    "111": "Independent values and no polynomial implementation guarantee; not an adversarial fee auction.",
    "113": "Matching samplers could generate constrained peers; not needed for fixed regular graph prototype.",
    "114": "Polymatroid rank oracles need a concrete network model before deployment.",
    "115": "Contingency-table sampling is a future heterogeneous capacity allocation candidate.",
    "116": "Identity testing is not a zero-knowledge proof system or hash construction.",
    "117": "Sparsest-cut hardness discourages general optimal cut claims; use finite certificates instead.",
    "118": "Bin-packing integrality gaps rule out assuming constant additive LP rounding for blocks.",
    "119": "Binary-noise information bounds do not provide a cryptographic secrecy proof.",
    "120": "Prescribed-degree factors are relevant to allowed-peer constraints; construction not ported.",
    "121": "Approximate edit distance does not authenticate ledger state.",
    "122": "Deletion-channel trace reconstruction differs from erasure-coded data availability.",
    "124": "Exactly three identical processors and unit jobs; transactions have heterogeneous gas costs.",
    "125": "Metric k-median could select relays; latency data and attacker model are absent.",
    "130": "Exact complex arithmetic with unrestricted coefficients is not a finite-field NTT speedup.",
    "136": "PPAD local verification is not a ready-made succinct zero-knowledge proof for computation.",
    "138": "Security watch: worst-case subset-sum algorithm; no new knapsack cryptosystem should rely on old costs.",
    "139": "Oracle query savings allow unrestricted computation; not wall-clock sampler performance.",
    "142": "Factoring polynomials over prime fields is not factoring integers or breaking Ed25519.",
    "174": "Thin trees may inform broadcast backbones; a single tree is fragile and no implementation is claimed.",
    "175": "Threshold decompositions are not directly a deterministic failure detector or routing scheme.",
    "177": "High-dimensional expansion is interesting for coding; no encoder/decoder/DA protocol supplied here.",
    "219": "Asymptotic random spectral statistics do not certify one deployed adversarial graph.",
    "229": "Random broadcast/reconstruction thresholds are not Byzantine voting thresholds.",
    "231": "Infinite IID spanning forests do not directly implement a finite authenticated overlay.",
    "238": "Shuffle TV mixing is not computational unpredictability; no secure beacon follows.",
    "265": "Quantum tensor-network approximation is not a ledger compression or validity proof.",
    "272": "Entanglement alone does not imply secret key; no quantum secrecy claim made.",
    "276": "Quantum-channel capacity differs from classical P2P bandwidth.",
    "277": "Entangled-game repetition does not yield a network quorum threshold.",
    "279": "Quantum integer factoring reinforces a migration concern; not an implemented classical attack or PQ signature.",
    "281": "Thermodynamic-first QAOA asymptotics are not a finite transaction-selection benchmark.",
    "283": "Unitary synthesis presupposes quantum/oracle resources; not classical validation acceleration.",
    "376": "Computation in forced fluid equations is not a practical proof-of-work or consensus construction.",
}


def clean(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", value)).strip()


def build() -> dict:
    contents = (ROOT / "CONTENTS.md").read_bytes()
    original = subprocess.check_output(["git", "show", f"{SOURCE_COMMIT}:CONTENTS.md"], cwd=ROOT)
    if original != contents:
        raise ValueError("source catalogue changed; explicitly rebase the research assessment")
    entries = re.findall(
        r"^\*\*(\d{3})\. (.*?)\*\*(.*?)(?=^\*\*\d{3}\.|\Z)",
        contents.decode(), re.M | re.S,
    )
    families, manuscripts = [], []
    for family, title, body in entries:
        assessment, reason = SELECTED.get(family, (
            "candidate-deferred" if family in CANDIDATES else "catalogue-only-not-selected",
            CANDIDATES.get(family, "No concrete protocol mapping selected in this prototype; not a proof of inapplicability."),
        ))
        links = list(re.finditer(r"&emsp;\[(.*?)\]\((preprints/.*?\.pdf)\)", body))
        families.append({
            "family": family, "title": clean(title),
            "catalogue_claim": clean(body.split("</td>", 1)[0]),
            "assessment": assessment, "reason": reason,
            "review_depth": "selected-source-and-definition-review" if family in SELECTED else "catalogue-screening-only",
            "formalization_scope": f"lean/docs/{family}.md" if (ROOT / f"lean/docs/{family}.md").exists() else None,
            "manuscripts": [link[2] for link in links],
        })
        for index, link in enumerate(links):
            pdf = ROOT / link[2]
            if not pdf.is_file():
                raise ValueError(f"missing manuscript {pdf}")
            end = links[index + 1].start() if index + 1 < len(links) else len(body)
            abstract = body[link.end():end].split("</td>", 1)[0]
            sources = sorted(pdf.parent.rglob("*.tex"))
            matches, digest = {}, hashlib.sha256()
            for source in sources:
                data = source.read_bytes()
                path = source.relative_to(ROOT).as_posix()
                digest.update(path.encode() + b"\0" + data + b"\0")
                lower = data.decode("utf-8", errors="replace").lower()
                hits = [term for term in TERMS if term in lower]
                if hits:
                    matches[path] = hits
            manuscripts.append({
                "family": family, "title": clean(link[1]), "pdf": link[2],
                "abstract": clean(abstract), "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                "tex_files_scanned": len(sources), "tex_bundle_sha256": digest.hexdigest(),
                "keyword_hits": matches, "proof_audit": "not-performed",
            })
    # Compare manuscript directories, rather than counting build PDFs twice.
    discovered = {p.relative_to(ROOT).as_posix() for p in (ROOT / "preprints").iterdir() if p.is_dir()}
    mapped = {str(Path(p["pdf"]).parent) for p in manuscripts}
    if discovered != mapped or len(mapped) != len(manuscripts):
        raise ValueError("catalogue/directory coverage is not one-to-one")
    return {
        "source_commit": SOURCE_COMMIT,
        "contents_sha256": hashlib.sha256(contents).hexdigest(),
        "method": "All catalogue entries/abstracts imported and all associated TeX scanned for fixed terms; NOT all proofs reviewed.",
        "terms": TERMS,
        "counts": {"families": len(families), "manuscripts": len(manuscripts),
                   "selected_families": len(SELECTED), "deferred_candidates": len(CANDIDATES),
                   "tex_files_scanned": sum(p["tex_files_scanned"] for p in manuscripts)},
        "families": families, "manuscripts": manuscripts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = build()
    encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path = OUT / "inventory.json"
    rows = ["# 全系列の一次スクリーニング台帳", "",
            "全証明の検証済み一覧ではない。原文の要旨・各論文のハッシュ・全文検索箇所は inventory.json を参照。", "",
            "|系列|題名|判定|理由|", "|---|---|---|---|"]
    for entry in data["families"]:
        rows.append(f"|{entry['family']}|{entry['title'].replace('|', '/')}|{entry['assessment']}|{entry['reason']}|")
    outputs = {path: encoded, OUT / "COVERAGE.md": "\n".join(rows) + "\n"}
    for target, content in outputs.items():
        if args.check:
            if not target.exists() or target.read_text() != content:
                raise SystemExit(f"inventory differs: {target}")
        else:
            target.write_text(content)
    print(json.dumps(data["counts"], sort_keys=True))


if __name__ == "__main__":
    main()
