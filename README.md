# `fj-mcp`

Servidor MCP por `stdio` para Forgejo, pensado para agentes que colaboran en desarrollo de software y reutilizan el CLI local `fj`.

## Herramientas expuestas

- `discover_pull_requests`
- `view_pull_request`
- `create_pull_request`
- `close_pull_request`
- `merge_pull_request`
- `approve_pull_request`
- `discover_repositories`
- `view_repository`
- `discover_issues`
- `view_issue`
- `create_issue`
- `comment_on_issue`

## Cómo ejecutarlo

```bash
uv run fj-mcp --default-host git.my-forgejo.com
```

También puedes usarlo con `uvx` sin instalación global:

```bash
uvx --from git+https://github.com/jairov4/fj-mcp.git fj-mcp --default-host git.my-forgejo.com
```

Ejemplo de configuración en `mcp.json`:

```json
{
  "mcpServers": {
    "fj": {
      "command": "uvx",
      "args": [
        "--from",
        "/ruta/a/fj-mcp",
        "fj-mcp",
        "--default-host",
        "git.my-forgejo.com"
      ],
      "env": {
        "FORGEJO_TOKEN": "tu_token_opcional_para_aprobar_prs"
      }
    }
  }
}
```

Variables opcionales:

- `FJ_MCP_DEFAULT_HOST`: host por defecto si no quieres pasarlo por argumento.
- `FJ_BIN`: ruta al binario `fj`.
- `FJ_MCP_NEUTRAL_CWD`: directorio neutro desde el que se ejecuta `fj` para evitar depender del repo actual.
- `FJ_MCP_APPROVAL_TOKEN_ENV`: nombre de la variable que contiene el token para aprobar PRs.

Si prefieres no usar `uvx`, el ejemplo anterior también funciona con:

```json
{
  "mcpServers": {
    "fj": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/jairov4/fj-mcp.git",
        "fj-mcp",
        "--default-host",
        "git.my-forgejo.co"
      ]
    }
  }
}
```

## Configuración de aprobación de PRs

`fj` no expone un subcomando para aprobar PRs, así que `approve_pull_request` usa la API HTTP de Forgejo.

Por defecto busca el token en `FORGEJO_TOKEN`:

```bash
export FORGEJO_TOKEN=tu_token
uv run fj-mcp --default-host git.my-forgejo.com
```

## Formatos importantes

Los issues y PRs se identifican como `owner/repo#123`.

`create_pull_request` soporta dos modos:

- Remoto, usando `repo`, `base` y `head`.
- Local, usando `use_current_repo: true` o `workdir`, útil cuando `fj pr create` necesita contexto del checkout.

## Notas

- El servidor ejecuta la mayoría de comandos desde un directorio neutro porque `fj` falla en repos con branch inicial no creada.
- Las respuestas de herramientas devuelven un JSON serializado con el comando ejecutado, `stdout`, `stderr` y `exit_code`, para que el agente pueda razonar sin depender de parseos frágiles.
- El paquete usa layout `src/` y expone el comando `fj-mcp`, por lo que `uv run fj-mcp` y `uvx --from /ruta/a/fj-mcp fj-mcp` funcionan directamente.
