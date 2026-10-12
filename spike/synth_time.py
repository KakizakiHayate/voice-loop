import sys, time, json, urllib.request, urllib.parse
port, speaker, name = sys.argv[1], sys.argv[2], sys.argv[3]
text = open('line.txt').read().strip()
t = time.time()
req = urllib.request.Request(f"http://127.0.0.1:{port}/audio_query?" + urllib.parse.urlencode({'text': text, 'speaker': speaker}), method='POST')
q = urllib.request.urlopen(req).read()
req = urllib.request.Request(f"http://127.0.0.1:{port}/synthesis?speaker={speaker}", data=q, headers={'Content-Type': 'application/json'}, method='POST')
wav = urllib.request.urlopen(req).read()
open(f'{name}.wav', 'wb').write(wav)
print(f"{name}: synth-only {time.time()-t:.1f}s, {len(wav)/2/24000:.1f}s of audio" if name=='voicevox' else f"{name}: synth-only {time.time()-t:.1f}s, {len(wav)/2/44100:.1f}s of audio")
