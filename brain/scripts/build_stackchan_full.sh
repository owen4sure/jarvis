#!/bin/bash
# 編譯 stackchan-mcp 的 stackchan board 韌體：
# 全螢幕臉 + Feetech 舵機(轉頭/跳舞) + yo bro 喚醒詞 + xiaozhi 協定(連我們的 server)
set -e
export IDF_PATH=~/esp/esp-idf
export IDF_PYTHON_CHECK_CONSTRAINTS=no
IDFENV=$(ls -d ~/.espressif/python_env/idf5.5* 2>/dev/null | head -1)
TOOLBIN=$(find ~/.espressif/tools -type d -name bin 2>/dev/null | tr '\n' ':')
export PATH="$IDFENV/bin:$IDF_PATH/tools:${TOOLBIN}/opt/homebrew/bin:$PATH"
export PYTHON=$IDFENV/bin/python
IDF="python $IDF_PATH/tools/idf.py"

cd ~/esp/stackchan-fw/firmware

cat > sdkconfig.defaults.local <<'CFG'
CONFIG_BOARD_TYPE_STACKCHAN=y
CONFIG_SPIRAM_MODE_OCT=n
CONFIG_SPIRAM_MODE_QUAD=y
CONFIG_CAMERA_GC0308=y
CONFIG_CAMERA_GC0308_AUTO_DETECT_DVP_INTERFACE_SENSOR=y
# CONFIG_CAMERA_GC0308_DVP_YUV422_640X480_16FPS is not set
CONFIG_CAMERA_GC0308_DVP_YUV422_320X240_20FPS=y

CONFIG_STACKCHAN_SERVO_FEETECH=y
# CONFIG_SEND_WAKE_WORD_DATA is not set
# 喚醒詞改用 WakeNet「Jarvis」（官方預訓練、~95% 可靠，遠勝 MultiNet 自訂 yo bro）
# CONFIG_USE_CUSTOM_WAKE_WORD is not set
# CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS is not set
CONFIG_SR_WN_WN9_JARVIS_TTS=y
CFG
rm -f sdkconfig
export SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.esp32s3;sdkconfig.defaults.local"

echo "▶ set-target esp32s3"
$IDF -DSDKCONFIG_DEFAULTS="$SDKCONFIG_DEFAULTS" set-target esp32s3
echo "▶ 確認關鍵設定"
grep -E "CONFIG_BOARD_TYPE_STACKCHAN|CONFIG_SPIRAM_MODE_QUAD|CONFIG_CUSTOM_WAKE_WORD=|CONFIG_STACKCHAN_SERVO" sdkconfig | head

echo "▶ 編譯中..."
$IDF -DSDKCONFIG_DEFAULTS="$SDKCONFIG_DEFAULTS" build

echo "▶ 合併 bin..."
cd build
python -m esptool --chip esp32s3 merge_bin -o merged-stackchan.bin @flash_args
echo "✅ 完成：$(pwd)/merged-stackchan.bin"
