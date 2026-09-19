"""meli — cliente de la API pública de Mercado Libre usada por el copiloto.

`client.py` habla el subconjunto verificado en `docs/ESPECIFICACION.md` §3 (un vendedor
autenticado); `oauth.py` es el baile de autorización y la rotación de tokens; `fake.py` es un
simulador en FastAPI del mismo subconjunto, para tests y para `copiloto demo` sin red.
"""
