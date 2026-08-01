#!/usr/bin/env node
/**
 * baostock 直连测试脚本（无需安装 baostock / 任何依赖，仅用 Node 内置模块）
 *
 * 用途：从本机（你的 Mac）直接测试能否连上 baostock 服务器并拉到行情。
 *       baostock 是私有 TCP 协议（非 HTTP），协议细节来自 baostock 官方源码逆向。
 *
 * 运行：
 *   node baostock_test.js
 * 可选环境变量覆��：
 *   CODE=sh.600000  START=2026-07-28  END=2026-07-31  HOST=public-api.baostock.com  PORT=10030
 *
 * 说明：沙盒里跑这个脚本会复现 “10001011 黑名单用户”（因为封的是沙盒出口 IP）。
 *      在你自己的 Mac 上跑，出口 IP 不同，若没被封就能看到真实行情数据。
 */

'use strict';

const net = require('net');
const zlib = require('zlib');

// ── 协议常量（取自 baostock 源码）──
const VERSION = '00.9.30';
const MSG_SPLIT = '\x01';                 // 0x01，消息字段分隔符
const HEADER_LEN = 21;                    // 头固定 21 字节
const SERVER_HOST = process.env.HOST || 'public-api.baostock.com';
const SERVER_PORT = parseInt(process.env.PORT || '10030', 10);
// 压缩响应类型：K线返回走 zlib 压缩
const COMPRESSED_TYPES = new Set(['96', '99', '9B', '9D']);
const MARKER = '<![CDATA[]]>\n';          // 压缩消息结尾哨兵（13 字节）

// ── 标准 CRC32（与 Python zlib.crc32 一致）──
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) {
      c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    }
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(buf) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) {
    c = CRC_TABLE[(c ^ buf[i]) & 0xFF] ^ (c >>> 8);
  }
  return (c ^ 0xFFFFFFFF) >>> 0;
}

// 把整数左补零到 len 位（baostock 头里的体长度字段）
function pad10(n) {
  let s = String(n);
  while (s.length < 10) s = '0' + s;
  return s;
}

// 拼一帧：header + body + \1 + crc + \n
function buildFrame(msgType, bodyStr) {
  const header = VERSION + MSG_SPLIT + msgType + MSG_SPLIT + pad10(bodyStr.length);
  const headBody = header + bodyStr;
  const crc = crc32(Buffer.from(headBody, 'utf-8'));
  return Buffer.from(headBody + MSG_SPLIT + String(crc) + '\n', 'utf-8');
}

// 读取一帧完整响应（区分压缩/非压缩结尾）
function readFrame(socket) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let buf = Buffer.alloc(0);
    const timer = setTimeout(() => socket.destroy(new Error('读取超时')), 20000);

    socket.on('data', (d) => {
      buf = Buffer.concat([buf, d]);
      if (buf.length < HEADER_LEN) return;
      const header = buf.slice(0, HEADER_LEN).toString('utf-8');
      const parts = header.split(MSG_SPLIT);
      const msgType = parts[1];
      const compressed = COMPRESSED_TYPES.has(msgType);
      if (compressed) {
        if (buf.length >= HEADER_LEN + 13 && buf.slice(-13).toString('utf-8') === MARKER) {
          clearTimeout(timer);
          resolve({ compressed, buf });
        }
      } else {
        if (buf[buf.length - 1] === 0x0A) {
          clearTimeout(timer);
          resolve({ compressed, buf });
        }
      }
    });
    socket.on('error', (e) => { clearTimeout(timer); reject(e); });
    socket.on('close', () => {
      clearTimeout(timer);
      if (buf.length) resolve({ compressed: COMPRESSED_TYPES.has(buf.slice(0, HEADER_LEN).toString('utf-8').split(MSG_SPLIT)[1]), buf });
      else reject(new Error('连接已关闭，未收到数据'));
    });
  });
}

// 把原始响应解析成 body 字段数组。
// 关键点：服务端(00.9.00)响应头固定 21 字节，其后即 body（body 内部才用 \x01 分隔）。
// 不能把整条消息按 \x01 切，否则会把头的 bodyLen 字段混入，导致错位。这与官方 Python 客户端一致。
function parseFrame(frame) {
  const header = frame.buf.slice(0, HEADER_LEN).toString('utf-8');
  const innerLen = parseInt(header.split(MSG_SPLIT)[2], 10);
  let bodyStr;
  if (frame.compressed) {
    const comp = frame.buf.slice(HEADER_LEN, HEADER_LEN + innerLen);
    bodyStr = zlib.unzipSync(comp).toString('utf-8');   // 解压后是 body + \1 + crc
  } else {
    bodyStr = frame.buf.slice(HEADER_LEN).toString('utf-8');
  }
  // 去掉结尾的 \n 与 <![CDATA[]]> 标记（标记尾随在 crc 字段后）
  if (bodyStr.endsWith('\n')) bodyStr = bodyStr.slice(0, -1);
  if (bodyStr.endsWith('<![CDATA[]]>')) bodyStr = bodyStr.slice(0, -13);
  return bodyStr.split(MSG_SPLIT);
}

function sendAndRecv(msgType, bodyStr) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host: SERVER_HOST, port: SERVER_PORT }, () => {
      socket.write(buildFrame(msgType, bodyStr));
    });
    readFrame(socket)
      .then((frame) => { socket.destroy(); resolve(parseFrame(frame)); })
      .catch((e) => { try { socket.destroy(); } catch (_) {} reject(e); });
  });
}

async function main() {
  const code = process.env.CODE || 'sh.600000';
  const start = process.env.START || '2026-07-28';
  const end = process.env.END || '2026-07-31';
  const fields = 'date,open,high,low,close,volume,amount';

  console.log(`目标服务器: ${SERVER_HOST}:${SERVER_PORT}`);
  console.log(`查询: ${code}  区间 ${start}~${end}  后复权(adjustflag=1)\n`);

  // 1) 登录（匿名）
  const loginBody = 'login' + MSG_SPLIT + 'anonymous' + MSG_SPLIT + '123456' + MSG_SPLIT + '0';
  let parts = await sendAndRecv('00', loginBody);   // 00 = 登录请求
  const loginCode = parts[0];
  const loginMsg = parts[1];
  console.log(`[登录] error_code=${loginCode}  error_msg=${loginMsg}`);
  if (loginCode !== '0') {
    console.log('\n=> 登录失败。若 error_code=10001011，说明本机出口 IP 被 baostock 拉黑。');
    return;
  }

  // 2) 查询历史 K 线（plus，压缩返回）
  const queryBody = [
    'query_history_k_data_plus', 'anonymous', '1', '2000', code,
    fields, start, end, 'd', '1',
  ].join(MSG_SPLIT);
  parts = await sendAndRecv('95', queryBody);       // 95 = K线plus请求
  const qCode = parts[0];
  const qMsg = parts[1];
  console.log(`[查询] error_code=${qCode}  error_msg=${qMsg}`);

  if (qCode !== '0') {
    console.log('\n=> 查询失败。');
    return;
  }

  // 3) 解析数据（parts[6] 是 JSON：{record: [[...], ...]}）
  try {
    const json = JSON.parse(parts[6]);
    const records = json.record || [];
    console.log(`\n=> 成功拉到 ${records.length} 行数据（前 10 行）：`);
    for (const row of records.slice(0, 10)) {
      console.log('   ', row.join('  '));
    }
  } catch (e) {
    console.log('\n=> 数据字段解析失败：', e.message);
    console.log('原始 parts[9] =', parts[9]);
  }
}

main().catch((e) => {
  console.error('运行出错：', e.message);
  process.exit(1);
});
