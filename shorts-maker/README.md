# shorts-maker

Transforma um trecho de vídeo (link ou arquivo) em um **Short vertical 1080x1920**, pronto para YouTube Shorts, TikTok e Reels. Um comando faz: baixar/cortar o trecho, converter para 9:16, gerar legenda e exportar o MP4.

## Instalação

Python 3.10+ e **ffmpeg** são obrigatórios. O resto é opcional:

| Ferramenta | Para quê | Instalar |
|---|---|---|
| ffmpeg | tudo | macOS: `brew install ffmpeg` · Windows: `winget install Gyan.FFmpeg` · Linux: `sudo apt install ffmpeg` |
| yt-dlp | baixar de links (YouTube, Twitch etc.) | `pip install -U yt-dlp` |
| faster-whisper | legenda automática (`--captions`) | `pip install faster-whisper` |

O script em si só usa a biblioteca padrão do Python.

## Uso

```bash
# Link: baixa só o trecho 12:30–13:10 (nunca a live inteira) e converte
python shorts_maker.py "https://www.youtube.com/watch?v=XXXX" --start 12:30 --end 13:10

# Arquivo local, com legenda automática em português
python shorts_maker.py meu_video.mp4 --start 1:10 --end 1:45 --captions

# Com título e crédito (gera um .txt com a descrição pronta)
python shorts_maker.py meu_video.mp4 --start 0 --end 40 --title "Reação ao trailer" \
    --credit "Canal X (com permissão)" --hashtags "#GTA6 #Shorts"

# Vários cortes de uma vez
python shorts_maker.py --batch clips.example.txt --captions

# Só mostrar os comandos, sem executar
python shorts_maker.py meu_video.mp4 --end 30 --dry-run
```

Os arquivos saem em `output/` (mude com `-o`). Tempos aceitam `SS`, `MM:SS` ou `HH:MM:SS`.

## Layouts (`--layout`)

- `blur` (padrão): o vídeo inteiro no centro, sobre o próprio vídeo ampliado e desfocado.
- `crop`: corta o centro e preenche a tela toda. Use `--crop-pos 0..1` para mover o corte para a esquerda/direita (bom quando a ação está num lado).
- `fit`: vídeo no centro com barras pretas.

## Legendas

- `--captions`: transcreve com Whisper e queima a legenda no vídeo, poucas palavras por vez (`--words-per-cue`, padrão 3). Escolha o idioma com `--lang` e o modelo com `--whisper-model` (`tiny` é rápido, `small` é o padrão, `medium` erra menos).
- `--srt arquivo.srt`: usa uma legenda sua; os tempos são relativos ao início do trecho.
- Estilo: `--font`, `--font-size` e `--caption-margin` (distância da borda inferior, em px). A margem padrão deixa a legenda acima da área coberta pelos botões do Shorts.

## Arquivo de lote

Uma linha por corte: `FONTE INÍCIO FIM [TÍTULO]`. Linhas com `#` são comentários. As demais opções (layout, legenda, crédito) valem para todas as linhas. Veja `clips.example.txt`.

## Direitos de uso

A ferramenta não verifica de quem é o conteúdo, e isso é responsabilidade sua. Use apenas material seu, com **permissão do criador** (combine por escrito) ou com licença que permita reuso. Baixar e repostar lives de terceiros sem autorização gera strikes e pode derrubar o canal.

Para conteúdo da Rockstar/GTA, a [política oficial](https://support.rockstargames.com/articles/200153756/Policy-on-posting-copyrighted-Rockstar-Games-material) tolera apenas uso **não comercial** e manda remover material de antes do lançamento. O YouTube também só monetiza reaproveitamento com [comentário ou edição originais](https://support.google.com/youtube/answer/1311392): adicione sua voz, análise ou humor, não só o corte.

## Limites e dicas

- Short pode ter até 3 minutos; o script avisa se o trecho passar disso.
- Os primeiros 2 segundos decidem se a pessoa fica: corte começando no momento forte, não na introdução.
- Se o yt-dlp parar de funcionar com um site, atualize: `pip install -U yt-dlp`.
