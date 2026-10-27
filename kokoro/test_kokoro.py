import kokoro_onnx
import soundfile as sf

kokoro = kokoro_onnx.Kokoro(
    "kokoro-v1.0.onnx",
    "voices-v1.0.bin"
)

samples, sample_rate = kokoro.create(
    "Привет. Я Джарвис. Система готова к работе.",
    voice="af_heart",
    speed=1.0,
    lang="ru"
)

sf.write("test.wav", samples, sample_rate)

print("Готово: test.wav")
