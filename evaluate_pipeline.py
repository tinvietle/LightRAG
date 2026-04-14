import json
import random
import urllib.request
import re
from pathlib import Path
import os
from dotenv import load_dotenv
load_dotenv()

CACHE_FILE = "data/rag_storage/kv_store_llm_response_cache.json"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gpt-oss:120b-cloud"
SAMPLE_SIZE = 3
TUPLE_DELIMITER = "<|#|>"

# CLINICAL_ENTITY_TYPES = {"Disease", "Symptom", "Drug", "Procedure", "LabTest", "LabFinding", "Anatomy", "Pathogen", "RiskFactor", "ClinicalSign", "Allergy", "Complication", "MedicalDevice", "Specialty"}
CLINICAL_ENTITY_TYPES = set(os.getenv("ENTITY_TYPES").split(",")) if os.getenv("ENTITY_TYPES") else set()

def parse_extraction_result(text):
    entities = []
    relations = []
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split(TUPLE_DELIMITER)
        if parts[0] == 'entity' and len(parts) >= 4:
            entities.append({
                "entity_name": parts[1].strip(),
                "entity_type": parts[2].strip(),
                "entity_description": parts[3].strip()
            })
        elif (parts[0] == 'relation' or parts[0] == 'relationship') and len(parts) >= 5:
            relations.append({
                "source_entity": parts[1].strip(),
                "target_entity": parts[2].strip(),
                "relationship_keywords": parts[3].strip(),
                "relationship_description": parts[4].strip()
            })
    return entities, relations

def calculate_proxy_metrics(entities, relations):
    metrics = {
        "entity_count": len(entities),
        "relation_count": len(relations),
        "duplicate_entity_rate": 0,
        "orphan_entity_rate": 0,
        "other_type_ratio": 0,
        "actionable_relations": 0
    }
    
    if not entities:
        return metrics
        
    entity_names = [e['entity_name'] for e in entities]
    unique_entities = set(entity_names)
    metrics['duplicate_entity_rate'] = (len(entity_names) - len(unique_entities)) / len(entity_names)
    
    other_types = [e for e in entities if e['entity_type'] not in CLINICAL_ENTITY_TYPES]
    metrics['other_type_ratio'] = len(other_types) / len(entities)
    
    related_entities = set()
    for r in relations:
        related_entities.add(r['source_entity'])
        related_entities.add(r['target_entity'])
        kw = r['relationship_keywords'].lower()
        if any(x in kw for x in ['treat', 'caus', 'manifest', 'indicat', 'diagnos']):
            metrics['actionable_relations'] += 1
            
    orphans = [e for e in unique_entities if e not in related_entities]
    metrics['orphan_entity_rate'] = len(orphans) / len(unique_entities)
    
    return metrics

def run_llm_judge(chunk_text, entities, relations):
    prompt = f"""You are a medical AI judge. Score the extraction quality of the clinical knowledge graph from the given text.

Text:
{chunk_text}

Extracted Entities:
{json.dumps(entities, indent=2)}

Extracted Relationships:
{json.dumps(relations, indent=2)}

Output strictly in JSON format as follows:
{{
  "entity_score": {{
    "relevance": <1-10 scale integer>,
    "clinical_usefulness": <1-10 scale integer>,
    "specificity": <1-10 scale integer>,
    "normalization_quality": <1-10 scale integer>
  }},
  "relationship_score": {{
    "clinical_correctness": <1-10 scale integer>,
    "reasoning_usefulness": <1-10 scale integer>,
    "directionality": <1-10 scale integer>,
    "text_support": <1-10 scale integer>
  }},
  "overall_assessment": "<Your short overall evaluation string>",
  "key_errors": ["<list of any errors>"],
  "recommendations": ["<list of recommendations>"]
}}
"""
    data = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.0}
    }
    
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode('utf-8'))
            return json.loads(result['response'])
    except Exception as e:
        print(f"Error calling Ollama judge: {e}")
        return {
            "error": str(e),
            "entity_score": {"relevance": 0, "clinical_usefulness": 0, "specificity": 0, "normalization_quality": 0},
            "relationship_score": {"clinical_correctness": 0, "reasoning_usefulness": 0, "directionality": 0, "text_support": 0},
            "overall_assessment": f"Failed to run LLM judge: {e}",
            "key_errors": [],
            "recommendations": []
        }

def main():
    if not Path(CACHE_FILE).exists():
        print(f"Cache file {CACHE_FILE} not found.")
        return

    with open(CACHE_FILE, encoding='utf-8') as f:
        cache_data = json.load(f)
        
    # Extract only valid 'extract' items
    extract_items = [v for v in cache_data.values() if v.get('cache_type') == 'extract']
    print(f"Found {len(extract_items)} extract tasks in cache.")
    
    samples = extract_items[:SAMPLE_SIZE] if len(extract_items) >= SAMPLE_SIZE else extract_items
    
    results = []
    
    for i, sample in enumerate(samples):
        print(f"Processing sample {i+1} - Chunk ID: {sample.get('chunk_id', 'N/A')}")
        raw_output = sample.get('return', '')
        # reconstruct the chunk text approximation from prompt
        chunk_text = "See original text" 
        if 'original_prompt' in sample:
             p = sample['original_prompt']
            #  print(f"Original Prompt: {p}...")
             if '<Input Text>' in p:
                 chunk_text = p.split('<Input Text>')[1].split('<Output>')[0].strip()
        # data = json.loads(chunk_text.strip().strip("```").strip())
        # print(f"Chunk Text (truncated): {chunk_text}...")
        entities, relations = parse_extraction_result(raw_output)
        metrics = calculate_proxy_metrics(entities, relations)
        
        downstream_ready = True
        if metrics['entity_count'] < 3 or metrics['actionable_relations'] == 0:
            downstream_ready = False
            
        judge_res = run_llm_judge(chunk_text, entities, relations)
        
        results.append({
            "sample_id": sample.get('chunk_id', f"sample_{i}"),
            "metrics": metrics,
            "downstream_ready": downstream_ready,
            "judge_result": judge_res
        })
        print(f"Sample {i+1} metrics: {metrics}")
        
    # Generate Markdown Report
    report = f"# Medical LightRAG Evaluation Report\n\n"
    report += f"## Scope and Dataset\n"
    report += f"- Evaluated {len(samples)} samples from `{CACHE_FILE}`\n"
    report += f"- Judge Model: {MODEL_NAME}\n\n"
    
    report += "## Extraction Pipeline Overview\n"
    report += "Entity and Relationship extraction is performed by `lightrag/operate.py:extract_entities()`. "
    report += "It invokes the LLM using prompts defined in `lightrag/prompt.py`. Output is passed through parsers relying on the `<|#|>` delimiter. \n\n"
    
    report += "## Aggregate Insights\n"
    avg_entity_count = sum(r['metrics']['entity_count'] for r in results) / len(results) if results else 0
    avg_rel_count = sum(r['metrics']['relation_count'] for r in results) / len(results) if results else 0
    report += f"- Average Entities per Chunk: {avg_entity_count:.1f}\n"
    report += f"- Average Relationships per Chunk: {avg_rel_count:.1f}\n\n"
    
    report += "## Per-Case Results\n"
    for r in results:
        report += f"### Sample `{r['sample_id']}`\n"
        report += f"**Proxy Metrics:**\n"
        report += f"- Entities: {r['metrics']['entity_count']}, Relations: {r['metrics']['relation_count']}\n"
        report += f"- Duplicate Entity Rate: {r['metrics']['duplicate_entity_rate']:.1%}\n"
        report += f"- Orphan Entity Rate: {r['metrics']['orphan_entity_rate']:.1%}\n"
        report += f"- Actionable Relations: {r['metrics']['actionable_relations']}\n"
        report += f"- Downstream Readiness Heuristic: {'✅ Ready' if r['downstream_ready'] else '❌ Insufficient'}\n\n"
        
        report += f"**LLM Judge Assessment:**\n"
        j = r['judge_result']
        report += f"- Overall: {j.get('overall_assessment', 'N/A')}\n"
        if not j.get('error'):
            report += f"- Entity Scores: {json.dumps(j.get('entity_score'))}\n"
            report += f"- Relationship Scores: {json.dumps(j.get('relationship_score'))}\n"
        report += f"- Errors Found: {', '.join(j.get('key_errors', []))}\n"
        report += f"- Recommendations: {', '.join(j.get('recommendations', []))}\n\n"
        
    report += "## Error Analysis and Fixes\n"
    report += "### Findings\n"
    report += "Based on standard LightRAG implementation, potential pitfalls include high orphan entity rates or inaccurate relation directionalities in complex clinical logic.\n\n"
    report += "### Recommended Optimization Fixes\n"
    report += "1. **Entity Normalization:** Enforce strict medical ontology parsing (e.g. SNOMED-CT matching).\n"
    report += "2. **Missing Relationships:** Consider multi-hop Gleaning or increasing `entity_extract_max_gleaning`.\n"
    report += "3. **Judge Model Constraints:** If local `gpt-oss:120b-cloud` fails to load, ensure appropriate hardware or quantizations.\n\n"
    
    with open('evaluation_report.md', 'w', encoding='utf-8') as f:
        f.write(report)
        
    print("Report generated at `evaluation_report.md`")

if __name__ == "__main__":
    main()
