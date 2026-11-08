# WallpaperSync — Documentação Técnica e Arquitetura

Este documento serve como referência completa para desenvolvedores, mantenedores ou inteligências artificiais (LLMs) que venham a assumir o suporte e a evolução deste ecossistema.

---

## 1. Visão Geral do Projeto

O **WallpaperSync** é uma suíte de automação desenvolvida para extrair a paleta de cores dinâmica **diretamente do papel de parede ativo** do ambiente de desktop (GNOME, GSettings ou arquivo local) e sincronizá-la com aplicativos e ferramentas do sistema operacional:

- **Discord / Equibop** (temas `midnight` e `system24` com suporte completo a Material You M3);
- **Prompt do Terminal** (`starship.toml`);
- **Emulador de Terminal GNOME** (`Ptyxis`);
- **Navegador Web Firefox** (estilização dinâmica de abas horizontais e verticais, barra de URL e navegação via `userChrome.css`);
- **Ambiente GNOME Desktop** (sincronização automática da chave nativa `accent-color` no GNOME 47+).

> [!NOTE]
> **Independência de Extensões**: O projeto não depende mais da extensão ChromaLeon nem de arquivos intermediários GTK (`custom-accent.css`). Toda a extração e cálculo de cores é realizada nativamente em Python via análise de imagem (Pillow) e quantização adaptativa tonal.

---

## 2. Arquitetura e Fluxo de Execução

```mermaid
flowchart TD
    A[GNOME Desktop: Troca de Papel de Parede] -->|GSettings picture-uri / picture-uri-dark| D[WallpaperSync: sync_theme.py]
    A2[Arquivo de Imagem Local / Alteração de Imagem] -->|Gio.FileMonitor / inotify| D
    
    subgraph D-Bus & Suspensão
        S1[systemd-logind: PrepareForSleep false] --> D
        S2[ScreenSaver: ActiveChanged false] --> D
        S3[Timer Monotônico: Clock Jump > 5s] --> D
    end

    subgraph Extração de Cores Material You
        D --> E1[Pillow / Quantização Adaptativa MedianCut]
        E1 --> E2[Fórmula M3: Saturação, Luminosidade e População]
        E2 --> E3[Cores de Destaque e Superfícies Tinted]
    end

    E3 -->|1. midnight.theme.css| F1[Equibop / Discord]
    E3 -->|2. system24.theme.css| F1
    E3 -->|3. starship.toml| F2[Starship Prompt]
    E3 -->|4. wallpapersync.palette| F3[Ptyxis Terminal]
    E3 -->|5. userChrome.css| F4[Navegador Firefox]
    E3 -->|6. accent-color| F5[GNOME 47+ Native Accent]
```

---

## 3. Componentes e Lógica de Implementação

### 3.1. Núcleo: `sync_theme.py`
O script central executa as seguintes etapas:
1. **Detecção do Papel de Parede (`get_current_wallpaper_path`)**:
   - Detecta o esquema de cores ativo (`org.gnome.desktop.interface color-scheme`).
   - Busca `picture-uri-dark` (no modo escuro) ou `picture-uri` no schema `org.gnome.desktop.background`.
   - Converte URIs `file://` para caminhos de sistema de arquivos e decodifica caracteres especiais.
   - Suporta arquivos `.xml` de slideshows do GNOME e fallbacks como `~/.fehbg` ou diretórios de papéis de parede comuns.
2. **Extração e Matemática de Cores (`extract_wallpaper_colors`)**:
   - Redimensiona a imagem para análise sub-20ms.
   - Aplica quantização adaptativa de 24 cores (MedianCut).
   - Avalia cada cor candidata através de pontuação de contraste e vivacidade para temas escuros:
     $$\text{Score} = \text{População}^{0.55} \times \text{Saturação}^{1.4} \times \text{FatorLuminosidade}$$
   - Calcula automaticamente o *tint intensity* (8% a 14%) com base na saturação média do wallpaper.
   - Suporta papéis de parede vetoriais em `.svg` (análise de gradientes e paradas de cor).
3. **Distribuição**:
   - Atualiza os arquivos no diretório do projeto e propaga para os destinos mapeados no sistema.

---

### 3.2. Discord / Equibop (Flatpak)

#### Temas Suportados
1. **`midnight.theme.css`**:
   - Estilo flutuante moderno com ilhas e painéis separados.
   - `--gap: 12px;` e bordas com cantos arredondados suaves.
2. **`system24.theme.css`**:
   - Estilo Material You (Material Design 3).
   - `--gap: 14px;`, cantos de 20px (`border-radius: 20px`), elevação com sombreamento suave, botões e barra de digitação em formato de pílula (*pill buttons*).

#### Links Simbólicos
Os temas em `/home/pedro/Imagens/Wallpapers/WallpaperSync/` estão linkados para a pasta de configuração do Equibop:
- `~/.var/app/org.equicord.equibop/config/equibop/themes/midnight.theme.css`
- `~/.var/app/org.equicord.equibop/config/equibop/themes/system24.theme.css`

#### ⚠️ Isolamento do Flatpak para o Equibop
- Para evitar problemas de renderização de janelas sem bordas no Electron/Wayland quando overrides de GTK estão presentes, mantenha o isolamento do Flatpak:
  ```bash
  flatpak override --user --nofilesystem=xdg-config/gtk-3.0 org.equicord.equibop
  flatpak override --user --nofilesystem=xdg-config/gtk-4.0 org.equicord.equibop
  ```

---

### 3.3. Prompt Starship & Terminal Ptyxis

1. **Starship (`~/.config/starship.toml`)**:
   - `sync_theme.py` atualiza a seção `[palettes.wallpapersync]` (e mantém `[palettes.chromaleon]` para retrocompatibilidade) e define `palette = 'wallpapersync'`, injetando a cor primária, variações e fundos tingidos nos componentes do prompt.
2. **Ptyxis (`~/.local/share/org.gnome.Ptyxis/palettes/`)**:
   - Gera as paletas `wallpapersync.palette` e `chromaleon.palette` com `Primary=true` utilizando o tom do wallpaper como cor de fundo da janela, barra de títulos e cursor.
   - Seta via GSettings (`org.gnome.Ptyxis.Profile`) a paleta padrão e todos os perfis cadastrados (`profile-uuids`) com o ID `wallpapersync`.

---

### 3.4. Navegador Web Firefox (`userChrome.css` & `user.js`)

1. **Descoberta de Perfis**:
   - Varre automaticamente `~/.config/mozilla/firefox/` e `~/.mozilla/firefox/`, analisando o `profiles.ini` para localizar perfis ativos.
2. **Ativação Automática de Estilos (`user.js`)**:
   - Garante a injeção da preferência `toolkit.legacyUserProfileCustomizations.stylesheets = true;` no arquivo `user.js` de cada perfil.
3. **Estilização Dinâmica (`userChrome.css`)**:
   - Injeta as cores da paleta Material You nas variáveis nativas do Firefox.
   - Fornece suporte tanto para o layout tradicional de **abas horizontais** quanto para o modo de **abas verticais** (*Vertical Tabs*), colorindo abas ativas, bordas de foco da barra de URL e botões em hover.

---

## 4. Automação e Ciclo de Vida (systemd & D-Bus)

### 4.1. Serviço do Usuário
O serviço roda sob a sessão do systemd do usuário:
- **Arquivo**: `~/.config/systemd/user/wallpapersync.service` (com link simbólico de compatibilidade `chromaleon-discord-sync.service`)
- **Comando executado**: `python3 -u /home/pedro/Imagens/Wallpapers/WallpaperSync/sync_theme.py --watch`

### 4.2. Detecção de Mudança de Wallpaper em Tempo Real
- **GSettings**: O script conecta sinais de monitoramento direto nas chaves `picture-uri` e `picture-uri-dark` (`org.gnome.desktop.background`) e `color-scheme` (`org.gnome.desktop.interface`).
- **File Monitor**: Cria um `Gio.FileMonitor` diretamente no arquivo de imagem do wallpaper ativo. Se uma ferramenta externa ou script sobrescrever a imagem, a atualização é acionada instantaneamente.

### 4.3. Retorno de Suspensão (Sleep / Wakeup / Lockscreen)
- **3 Camadas de Detecção**:
  1. **D-Bus do Sistema (`systemd-logind`)**: Escuta o sinal `org.freedesktop.login1.Manager.PrepareForSleep`. Quando o booleano for `false` (acordando), aciona a sincronização.
  2. **D-Bus da Sessão (`ScreenSaver`)**: Escuta `ActiveChanged` de `org.gnome.ScreenSaver` e `org.freedesktop.ScreenSaver`. Quando o valor for `false` (tela desbloqueada), aciona a sincronização.
  3. **Salto Temporal (Fallback)**: Um timer de 1 segundo compara `time.monotonic()`. Se a diferença entre dois ciclos for maior que 5 segundos, a máquina comprovadamente esteve suspensa e acabou de retomar.

---

## 5. Mapeamento de Arquivos e Diretórios

| Arquivo / Caminho | Origem / Destino | Função |
|---|---|---|
| `/home/pedro/Imagens/Wallpapers/WallpaperSync/sync_theme.py` | Código-fonte principal | Daemon de monitoramento, conversão de cores e controle D-Bus |
| `/home/pedro/Imagens/Wallpapers/WallpaperSync/midnight.theme.css` | Gerado por `sync_theme.py` | Tema Discord Midnight sincronizado |
| `/home/pedro/Imagens/Wallpapers/WallpaperSync/system24.theme.css` | Gerado por `sync_theme.py` | Tema Discord System24 (Material You M3) sincronizado |
| `/home/pedro/Imagens/Wallpapers/WallpaperSync/firefox-userChrome.css` | Gerado por `sync_theme.py` | Cópia local de referência do tema do Firefox |
| `~/.config/systemd/user/wallpapersync.service` | Serviço systemd | Autostart do monitoramento no login do usuário |
| `~/.var/app/org.equicord.equibop/config/equibop/themes/` | Links simbólicos | Diretório onde o Equibop carrega os temas |
| `~/.config/starship.toml` | Arquivo do sistema | Configuração do prompt do shell |
| `~/.local/share/org.gnome.Ptyxis/palettes/wallpapersync.palette` | Paleta gerada | Paleta dinâmica para o terminal Ptyxis |
| `~/.config/mozilla/firefox/<perfil>/chrome/userChrome.css` | Arquivo gerado | Estilos dinâmicos de interface para o Firefox |
| `~/.config/mozilla/firefox/<perfil>/user.js` | Arquivo de preferências | Ativa a leitura do `userChrome.css` no Firefox |

---

## 6. Guia de Operação e Linha de Comando (CLI)

### Comandos Rápidos

- **Executar sincronização única (utilizando o wallpaper ativo atual)**:
  ```bash
  python3 sync_theme.py
  ```

- **Ver informações do papel de parede detectado e cores calculadas**:
  ```bash
  python3 sync_theme.py --info
  ```

- **Sincronizar a partir de uma imagem de wallpaper específica**:
  ```bash
  python3 sync_theme.py --image /caminho/para/meu_wallpaper.jpg
  ```

- **Iniciar monitoramento em tempo real**:
  ```bash
  python3 sync_theme.py --watch
  ```

- **Instalar o serviço do systemd para iniciar automaticamente com a sessão**:
  ```bash
  python3 sync_theme.py --install-service
  ```

- **Verificar status do serviço em segundo plano**:
  ```bash
  systemctl --user status wallpapersync.service
  ```

- **Ver logs em tempo real**:
  ```bash
  journalctl --user -u wallpapersync.service -f
  ```

- **Reiniciar o serviço após alterações**:
  ```bash
  systemctl --user restart wallpapersync.service
  ```
