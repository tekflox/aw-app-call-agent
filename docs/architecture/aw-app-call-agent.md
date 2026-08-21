---
repo: architecture
path: docs/architecture/aw-app-call-agent.md
source: generated
edited: false
checksum: sha256:a5d0174444176b64525593068b664473d290d86da7493d4abc6bf9621f722145
---
# Call Agent

- **repo**: aw-app-call-agent
- **layer**: app
- **technologies**: python, react
- **health** (derived): planned

Talk to your workspace agent out loud. Open the Call window, hit call, and speak — your voice is transcribed, sent to the agent you picked, and the reply is streamed back and spoken to you in your own language. Keeps the conversation going across calls instead of starting from scratch every time.

## Connections
- `http` → **aw-workspace** — routes mounted at /api/apps/call-agent
- `other` → **aw-app-agents-platform-runners** — Holds the agents-platform base URL and identity token this app falls back to when its own are blank, and runs the agent CLI a call is dispatched to

## MCP tools
_none exposed_

## Requirements
### Backend não configurado é avisado no próprio socket, não só no log
- Given nem config do app, nem variável de ambiente, nem app de runners de onde herdar as credenciais
- When alguém abre a chamada mesmo assim (repos/aw-app-call-agent/call_agent_app/routes.py, rota /ws/call, verificado por repos/aw-app-call-agent/tests/test_routes.py::test_unconfigured_backend_errors_on_the_socket_not_in_a_log:150)
- Then o primeiro frame é um error nomeando a URL base da agents-platform como o que falta, e o ready vem logo depois, mantendo o socket utilizável — numa chamada de voz ninguém está olhando log: o sintoma seria um agente que atende e nunca responde, indistinguível de rede ruim. Dizer no canal em que a pessoa está é a única forma de o erro chegar até ela
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-call-agent/tests/test_routes.py` (passing)

### Config em branco herda as credenciais do app de runners, e diz que herdou
- Given o app de runners já guarda a base e o token da agents-platform, e pedir para configurar de novo duplicaria um segredo que já existe
- When as settings são resolvidas sem config e sem env (repos/aw-app-call-agent/call_agent_app/settings.py::_runners_config, via tests/test_routes.py::test_blank_config_inherits_from_the_runners_app:138)
- Then base e token vêm do app de runners e credentials_source informa "inherited:agents-platform-runners" — expor a PROCEDÊNCIA é o que evita a próxima meia hora de confusão: sem esse campo, uma configuração vazia que mesmo assim funciona parece bug, e alguém acaba colando um token para "consertar" o que já estava certo, criando a segunda cópia que vai ficar velha
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-call-agent/tests/test_routes.py` (passing)

### O texto da pessoa sempre chega ao prompt, mesmo com template sem placeholder
- Given o template do prompt é editável pelo usuário e pode simplesmente não conter o marcador ${text}
- When o prompt é montado (repos/aw-app-call-agent/call_agent_app/settings.py::CallSettings.build_prompt, via tests/test_routes.py::test_prompt_template_always_carries_the_users_words:181)
- Then o que a pessoa falou aparece no resultado de qualquer jeito, e com o placeholder presente a substituição é exata — um template mal editado que engolisse a fala produziria um agente que responde com convicção a uma pergunta que nunca ouviu, que é o pior comportamento possível num canal de voz, porque soa perfeitamente normal
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-call-agent/tests/test_routes.py` (passing)

### Markdown é retirado antes da fala e a voz cai num padrão conhecido
- Given a resposta do agente vem em markdown e o idioma pode chegar como pt-BR, en_US com underscore (forma que o iOS manda), vazio ou desconhecido
- When o texto é limpo e a voz escolhida (repos/aw-app-call-agent/call_agent_app/speech.py::strip_markdown e pick_edge_voice, via tests/test_routes.py::test_markdown_is_stripped_before_speech:177 e test_voice_picking:173)
- Then asteriscos, crases, colchetes de link e sustenidos somem antes da síntese, e idioma vazio ou desconhecido cai em pt-BR em vez de falhar — sem a limpeza o TTS pronuncia a pontuação em voz alta, e sem o fallback um idioma não mapeado deixaria a chamada muda, que é pior do que falar no idioma errado
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-call-agent/tests/test_routes.py` (passing)

### As settings nunca devolvem o token, só se ele existe
- Given a rota de settings alimenta a janela do app e o token da agents-platform está entre as credenciais que ela conhece
- When a resposta é montada (repos/aw-app-call-agent/call_agent_app/routes.py, rota /settings, verificado por tests/test_routes.py::test_settings_never_leaks_the_token:89)
- Then sai has_token booleano e nunca o valor — a UI só precisa saber se está configurado para decidir o que desenhar, e devolver o segredo o colocaria no devtools de qualquer pessoa com a janela aberta, além de qualquer log de proxy no caminho. É a mesma postura do /status do aw-app-aws, e vale a pena que os apps concordem nisso
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-call-agent/tests/test_routes.py` (passing)
