# Agente local — Descarga de XM

El FTP de XM (`xmftps.xm.com.co:210`) solo acepta conexiones desde IPs
conocidas — el servidor de la plataforma no puede llegar ahí directo. Por
eso la conexión real la hace este agente, corriendo en el computador de
quien lo use (que sí tiene acceso). **Cualquiera del equipo con
usuario/clave del FTP de XM puede usarlo**, no solo Jessica.

## Uso

1. Doble clic en `iniciar_descarga_xm.bat`. La primera vez instala unas
   dependencias (tarda unos segundos); luego arranca casi al instante.
2. Deja la ventana abierta.
3. Abre la pestaña "Descarga de XM" en la plataforma, desde el mismo
   computador, y úsala normal — el navegador habla directo con este
   agente en `http://127.0.0.1:8420`. Ahí pides usuario/clave del FTP de
   XM (nunca se guardan en el servidor ni en este repo).
4. Cuando termines, cierra la ventana del agente.

Si la pestaña muestra "No se pudo conectar con el agente local", hay dos
causas y se ven idénticas desde el navegador:

1. La ventana del `.bat` está cerrada o con errores.
2. **La dirección desde la que abriste la plataforma no está permitida.**
   El agente solo atiende a las páginas de su lista blanca; a las demás
   les responde `400 Disallowed CORS origin`, que el navegador reporta
   como si el agente no existiera. Al arrancar, el `.bat` imprime qué
   páginas acepta — compáralo con la URL de tu barra de direcciones.

Cualquier `localhost` está permitido siempre (5173 del front legacy, 3000
del v2). Para usar la pestaña desde la plataforma **desplegada** hay que
poner su dirección en `XM_ORIGENES_EXTRA` (ver abajo). Esto pasó cuando el
front se migró a Nuxt y otra vez cuando el despliegue se mudó de Vercel.

## Configuración (opcional)

Copia `.env.example` a `.env` en esta misma carpeta:

- `XM_ORIGENES_EXTRA` — la dirección de la plataforma desplegada, tal cual
  sale en la barra del navegador. Varias, separadas por coma. Sin esto la
  pestaña solo funciona desde un front local.
- `XM_CACHE_DIR` — dónde guardar los archivos descargados (ver abajo).

## Requisitos

Python 3.10+.

## Carpeta de caché

Los archivos que se descargan de XM se guardan en disco para no volver a
pedirlos si repites un rango — por defecto, en una carpeta bajo tu
usuario de Windows (`Documentos\Xm\Archivos_Filezilla`, o la carpeta
específica de Jessica si tu usuario de Windows es `jessi`). Para elegir
otra carpeta, pon `XM_CACHE_DIR` en el `.env`.
