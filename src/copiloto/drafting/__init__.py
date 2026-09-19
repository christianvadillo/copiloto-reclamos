"""drafting — redacción de la respuesta al comprador.

`templates.py` son plantillas fijas en español de México (siempre disponibles, sin red);
`guardrails.py` valida CUALQUIER mensaje (venga de plantilla o de LLM) antes de guardarlo;
`llm.py` habla con Claude para producir un borrador más natural; `drafter.py` orquesta:
LLM → guardrails → un reintento con las violaciones → plantilla si todo lo demás falla.
"""
