# Zadarma + Asterisk + Call Agent

## Fluxo

```text
telefone / iPhone -> número +351 30... -> Zadarma -> Asterisk
  -> AudioSocket 127.0.0.1:9019 -> Call Agent -> gravação WAV + histórico
```

O Asterisk e o Call Agent rodam no mesmo container Tier 2 e usam
`127.0.0.1:9019` para AudioSocket. AMI (`5038`) fica somente no loopback do
container. O manifest publica SIP `5060/udp` e RTP `10000-10100/udp`.

## Teste interno sem provedor

Em workspaces AW hospedados, deixe **Public SIP address** como `auto`: a app
resolve o hostname público do próprio workspace e configura o NAT/SDP do
Asterisk na inicialização. Em instalações self-hosted, informe o IP ou domínio
público alcançável pelo telefone. A app gera as senhas do ramal e do AMI
automaticamente. No softphone, registre o usuário mostrado em
**Softphone extension** (padrão `101`) contra o IP LAN, porta UDP 5060, usando
a senha do Settings. Disque **Call Agent extension** (padrão `700`). Nenhuma
conta Zadarma ou número público participa desse caminho.

## Dados que virão da Zadarma

- SIP registrar: `sip.zadarma.com`
- SIP login: o número SIP, não o e-mail da conta
- SIP password: a senha em **Ajustes SIP**, não a senha da conta
- número público: em formato E.164, por exemplo `+35130...`

## Call Agent > Settings

Preencha:

- **Enable SIP telephony**: ligado somente quando tudo abaixo estiver pronto
- **SIP registrar**: `sip.zadarma.com`
- **Zadarma SIP login/password**: credenciais SIP da área pessoal
- **Portuguese public number**: `+35130...`
- **AMI host/port**: `127.0.0.1:5038`
- **AMI username**: `call-agent`
- **AMI secret**: uma senha local forte
- **AI audio bridge**: `127.0.0.1:9019`

Abra **Asterisk config preview** no app e instale os blocos gerados em
`pjsip.conf`, `extensions.conf` e `manager.conf`. Recarregue PJSIP, dialplan e
AMI. A rota de entrada envia o DID para o contexto do Call Agent; a saída é
originada pelo botão **Ligar** usando AMI.

## Como ligar para o Call Agent

Depois de a Zadarma encaminhar o número direto para o tronco registrado,
ligue de qualquer telefone para o número português `+351 30...`. Para testar
de um softphone sem usar a rede pública, crie uma extensão separada no
Asterisk e disque a extensão do contexto de entrada do Call Agent.

## iPhone

Use **Linphone** como cliente gratuito do Asterisk. Crie uma extensão PJSIP
separada para o iPhone; nunca reutilize o login do tronco Zadarma. Configure o
domínio/IP do Asterisk, usuário e senha dessa extensão, preferencialmente com
TLS/SRTP, firewall e proteção contra brute force. Para um teste direto do
provedor, o aplicativo oficial da Zadarma é mais simples, mas ele não passa
automaticamente pelo Call Agent.

## Gravações e privacidade

O Call Agent guarda metadados em SQLite e áudio mono WAV em
`.aw-workspace/data/call-agent/`. O painel lista as chamadas e reproduz a
gravação autenticada. Antes de usar em produção, adicione uma mensagem de
aviso no dialplan e defina finalidade, acesso e prazo de retenção. Chamadas
gravadas são dados pessoais; o interlocutor deve receber as informações
aplicáveis e ter um caminho para exercer os seus direitos.
