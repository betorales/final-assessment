# Proyecto final de grado - Analista programador

> Plataforma web para el triaje de quimiotecas con perfiles de filtro configurables, que explica cada uno de sus descartes. 
> 
> Flask · PostgreSQL · RDKit · JavaScript

## Idea: de dónde nace

Luego de terminar mi carrera de pregrado en la escuela de farmacia, pasé tiempo buscando áreas para estudiar mi posgrado (que tengo pendiente). Nunca fui un estudiante bueno en química (sí, aunque no lo crean), pero entendía bastantes cosas, especialmente las relacionadas al contacto entre la molécula y el cuerpo. Luego de leer mucha literatura, me topé con el área de quimioinformática y descubrimiento de fármacos, pero no sin antes pasar por bioinformática, biología de sistemas y breve literatura sobre diseño de algoritmos.
 
Me enamoré de esa área. Fusionar el conocimiento farmacológico de la escuela de farmacia con entender *por qué sucede lo que sucede*, en lugar de solo leer lo que aparece en la pantalla, es **mágico**.
 
Entonces, ¿por qué llego hasta aquí? Durante el diseño de fármacos, quienes hemos leído un poco sobre esto sabemos que existen moléculas gigantes (muy por encima de los 500 Da) sobre las que uno se pregunta por qué no podrían pasar a candidatos para producción, rompiendo las reglas que hemos leído (p. ej.: Lipinski). O incluso, simplemente, cómo explicar a nuestros estudiantes (también soy docente) lo que sucede durante el proceso de cribado. Por eso surge esta idea que me gustaría llevar a cabo, por ahora es un *gustaría*, para abrir el conocimiento del proceso de descubrimiento de fármacos.

## Propuesta de valor

Esto lo extraigo directamente desde mi escrito del proyecto final:

> El proyecto ofrece a los equipos de investigación un cribado de quimiotecas que se adapta al tipo de molécula evaluada y que explica cada una de sus decisiones. Su valor no está en calcular descriptores, sino en cómo se usan para decidir


## ¿Por qué esta solución?

Como lo indicaba más arriba, a veces, como investigadores y/o docentes, uno necesita saber **«¿por qué esta molécula no pasa?»** o **«¿cómo puedo explicar esto?»**.
 
Durante el proceso de enseñanza-aprendizaje, que todos hemos vivido, una solución que te muestre por qué algo falla o por qué algo resulta es la mejor compañía.

## El caso que lo explica todo: Ciclosporina A

Si tuviera que defender este proyecto con una sola molécula, elegiría esta.
 
La **ciclosporina A** es un péptido cíclico de 11 aminoácidos. Está aprobada desde **1983** y se administra **por vía oral**. Es un fármaco real, viejo y en uso. Estos son sus descriptores, calculados con RDKit a partir de su SMILES canónico:
 
| Descriptor | Valor | Límite de Lipinski / Veber |
|---|---|---|
| Peso molecular | **1202,64 Da** | ≤ 500 |
| LogP | 3,27 | ≤ 5 |
| Donores de H | 5 | ≤ 5 |
| Aceptores de H | **12** | ≤ 10 |
| TPSA | **278,80 Å²** | ≤ 140 |
| Enlaces rotables | **15** | ≤ 10 |
| Átomos pesados | 85 | — |
 
Cuatro violaciones. Pesa **más del doble** del límite de Lipinski. Cualquier filtro rígido la descarta sin pestañear.
 
Y hay más. Al consultarla en **ChEMBL** (CHEMBL160), la base la clasifica como *proteína* y devuelve casi todos sus descriptores vacíos: ni LogP, ni TPSA, ni donores, ni aceptores. Solo informa el peso. Al probarla en **SwissADME**, la herramienta no la procesó por el tamaño de su SMILES; es coherente con su diseño, ya que esa plataforma está declarada explícitamente para el espacio de las moléculas pequeñas en química medicinal.
 
Ahí está la brecha. **Las herramientas de referencia están construidas para el espacio de las moléculas pequeñas, y dejan afuera a fármacos orales aprobados** del espacio *beyond Rule of 5* (bRo5): macrociclos, péptidos cíclicos, PROTACs.
 
¿Qué hace este sistema con la misma molécula?
 
- Con el perfil **Lipinski + Veber** → **rechazada**, y el sistema dice exactamente por qué: MW 1202,64 > 500; HBA 12 > 10; TPSA 278,80 > 140; RotB 15 > 10.
- Con el perfil **beyond Ro5** → **aprobada**, con una violación tolerada (TPSA 278,80 > 250, y ese perfil admite hasta dos).
La misma molécula, dos respuestas distintas, y en ambos casos el motivo queda escrito y guardado. Eso es lo que el sistema aporta: no el cálculo, sino la decisión explicada y trazable.
 
Una aclaración que me importa, porque es la que me permite dormir tranquilo: **el sistema clasifica, no predice.** Informa «cumple» o «no cumple el perfil X». Nunca dice que una molécula se absorbe. De hecho, la ciclosporina es el ejemplo clásico de molécula *camaleónica*: forma puentes de hidrógeno intramoleculares que ocultan su polaridad al atravesar membranas, de modo que su TPSA calculada sobreestima lo polar que se comporta en realidad. Un descriptor no es un destino.

## Qué hace el sistema

- **Carga de quimiotecas** desde archivos SMILES, con validación línea por línea y deduplicación por SMILES canónico.
- **Cálculo de descriptores** con RDKit sobre la estructura, no importados de la fuente externa (que, como vimos, los deja vacíos justo en los casos interesantes).
- **Perfiles de filtro configurables**, almacenados como datos y no como código: Lipinski, Veber, Lead-like, Fragmentos (Ro3), beyond Ro5, CNS, más los que cada usuario cree.
- **Alertas estructurales** por subestructura (PAINS y BRENK), con severidad diferenciada: PAINS descarta, BRENK solo advierte.
- **Trazabilidad del descarte**: cada rechazo guarda qué regla se incumplió y con qué valor.
- **Dos perfiles de usuario**: el investigador aplica los criterios, el curador científico los define.

---

## Documentación técnica

### Stack

| Capa | Tecnología |
|---|---|
| Backend | Flask + Jinja2 |
| ORM y migraciones | SQLAlchemy + Flask-Migrate |
| Base de datos | PostgreSQL |
| Quimioinformática | RDKit |
| Autenticación | Flask-Login + Werkzeug |
| Formularios y CSRF | Flask-WTF |
| Frontend | CSS propio (Grid y Flexbox) + JavaScript vanilla |

> [!NOTE] 
> Sin Bootstrap, sin plantillas y sin React. 
> Las dos primeras ausencias las pide la consigna de la asignatura. 
> La tercera es una decisión: Flask con Jinja2 ya cubre front y back, y agregar una segunda aplicación con su propio build no aportaría nada a lo que se evalúa. 
> La interactividad que el proyecto necesita (ordenar la tabla, filtrar, barra de progreso) se resuelve con unas pocas líneas de JavaScript.

### Rendimiento: carga por lotes

Cargar una quimioteca es la operación más costosa del sistema: cada SMILES se valida, se canonicaliza, se le calculan sus descriptores y se compara contra las 480 alertas PAINS. Al medirla con 500 compuestos, el mismo código se comportó muy distinto según el entorno:

| Entorno | Compuestos | Tiempo |
|---|---|---|
| Local (MacBook Air) | 500 | 2 s |
| Render, plan gratuito (0,1 CPU) + Neon (misma región, Ohio) | 500 | ~30 s, justo en el límite de Gunicorn (30 s) |

Para saber dónde se iba el tiempo, se midió cada etapa en producción:

| Etapa | Tiempo | Proporción |
|---|---|---|
| Descriptores (RDKit) | 10,6 s | 38 % |
| Alertas estructurales (480 patrones × 500 moléculas, RDKit) | 9,4 s | 33 % |
| Base de datos (consultas a Neon) | 8,1 s | 29 % |
| **Total** | **28,1 s** | |

El 71 % del tiempo es cálculo químico con una décima de CPU, no la base de datos. Optimizar solo las consultas habría ahorrado unos 7 segundos, y una biblioteca de 1000 compuestos igual habría superado el límite. Por eso se eligió **dividir el trabajo en lotes**:

1. El navegador lee el archivo y lo divide en tramos de 100 líneas (JavaScript, `static/upload.js`).
2. `POST /libraries/start` crea la biblioteca vacía.
3. Cada tramo se envía a `POST /libraries/<id>/batch`, que lo procesa y hace su propio `commit`. Los números de línea se conservan, así que los motivos de rechazo siguen apuntando a la línea real del archivo.
4. El resumen se acumula en la sesión, y al terminar se muestra como siempre. Una barra de progreso informa el avance.

Resultado en producción, con 1000 compuestos nuevos (10 lotes de 100 líneas y uno final de 3):

| Medición | Valor |
|---|---|
| Tiempo por lote | entre 5,09 s y 7,10 s (promedio ≈ 6,1 s) |
| Lote más lento comparado con el límite de 30 s | 24 % |
| Total | ≈ 61 s |

Ambos criterios de RNF-01 se cumplen: cada lote de 100 responde en menos de 30 s, y 500 compuestos se procesan en menos de 5 minutos.

> [!NOTE]
> Se descartó una cola de tareas (Celery o RQ) porque requiere un proceso *worker* y un *broker* como Redis: dos servicios más que desplegar y mantener. Para el volumen del proyecto, los lotes resuelven el problema. Sin JavaScript, el formulario sigue funcionando y sube el archivo en una sola petición.

### Límites declarados
 
Todo software de quimioinformática tiene un límite. Los de este están documentados y se comunican al usuario, en lugar de fallar en silencio:
 
1. **Máximo 200 átomos pesados.** Por encima, el compuesto se carga pero se marca como fuera de alcance y no se le calculan descriptores. (La ciclosporina tiene 85, así que entra holgada.)
2. **LogP es un valor calculado**, no experimental. Algoritmos distintos dan resultados distintos para la misma molécula.
3. **Las alertas PAINS son heurísticas.** Hay fármacos aprobados que contienen subestructuras marcadas, y el sistema las detecta: la **levodopa** y la **entacapona** (Parkinson) activan `catechol_A(92)`, y el **epalrestat** (neuropatía diabética) activa `ene_rhod_A(235)`. Por eso el curador puede desactivar una alerta, y cada descarte por alerta queda escrito con su nombre.
4. **El sistema no predice ADME.** Clasifica según criterios explícitos y guarda cuáles fueron.

### Fuera de alcance de esta entrega
 
- Modelos QSAR para predicción de actividad.
- Descriptores dependientes de conformación 3D y acoplamiento molecular.
- Importación en línea desde la API de ChEMBL.
- Validación experimental de los perfiles contra datos de biodisponibilidad.

---

## Decisiones de diseño

**Alertas estructurales**

1. Las alertas no suman violaciones. `violations` sigue contando solo reglas fisicoquímicas (MW, LogP, etc.). Una alerta es otro tipo de problema, y mezclarlos haría que `allowed_violations` perdiera su sentido.
2. Una alerta de severidad `discard` rechaza el compuesto sin importar cuántas violaciones tolere el perfil. Una de severidad `warning` solo queda escrita en el motivo.
3. Solo cuentan las alertas activas, porque el curador puede desactivar una alerta que considere demasiado estricta.
4. `evaluate` no consulta la base directamente. Las alertas le llegan a través del compuesto.
5. Los 480 patrones PAINS se cargan desde el archivo que trae RDKit (`Data/Pains/wehi_pains.csv`). Contienen hidrógenos explícitos, por eso se compilan con `mergeHs=True`. Así compilados, dan los mismos resultados que el catálogo PAINS interno de RDKit sobre sus 10.000 moléculas de prueba. BRENK todavía no está cargado: RDKit no lo incluye como texto SMARTS.
6. La tabla `compound_alert` es un dato derivado. Se calcula una sola vez, al cargar el compuesto, y no durante el cribado. Registra todas las alertas, incluso las inactivas, para que activar o desactivar una no obligue a recalcular nada. El comando `rebuild-alerts` la reconstruye por completo y puede ejecutarse las veces que haga falta.

**Bibliotecas y compuestos**

1. Al borrar una biblioteca no se borran sus compuestos. Para eso existe la deduplicación por SMILES canónico: un compuesto puede estar en varias bibliotecas y se reutiliza. Lo que sí se borra, en cascada, son sus vínculos y sus cribados.
2. Los descriptores y las alertas se calculan solo para los compuestos nuevos. Si un compuesto ya existe en la base, se reutiliza tal cual.
3. Con la carga por lotes, cada lote se confirma por separado. Si un lote falla, los anteriores quedan guardados y el usuario ve en qué línea se detuvo la carga; desde ahí puede borrar la biblioteca e intentar de nuevo.

**Cribado y trazabilidad**

1. Cada cribado guarda sus resultados en el momento de ejecutarse y no se recalcula después. Si una alerta se desactiva, los cribados anteriores conservan la decisión que se tomó con las reglas de ese momento.
2. Limitación conocida: el cribado guarda el identificador del perfil, no una copia de sus umbrales. Si el perfil se edita después, no queda registrada la configuración exacta que se aplicó. Una mejora posible es guardar esa configuración en una columna JSONB del cribado.

**Seguridad**

1. Los permisos se comprueban en el servidor, en cada ruta. Ocultar un botón en la interfaz no es seguridad: un formulario se puede enviar a mano.
2. Un recurso ajeno (una biblioteca o un cribado de otro usuario) responde **404** y no 403, para no confirmar que existe. Una página reservada al curador responde **403**, porque su existencia no es un secreto.
3. Un curador no puede desactivar su propia cuenta ni cambiar su propio rol, para que el sistema nunca quede sin nadie que lo administre.

---

## Autor
 
Alberto Morales - Farmacéutico y docente, estudiante de Analista Programador.
 
Proyecto individual de final de grado.