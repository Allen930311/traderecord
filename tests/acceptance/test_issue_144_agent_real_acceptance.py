"""Issue #144: Read-only real evidence regression, plus an intentionally strict E2E release gate.

The below checks MUST NOT write to canonical/. The last case is only green after
actual canonical BT registration and evidence readback. A passing chart-template
unit test is not a substitute for genuine Fresh Agent execution.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from trading_os.backtest_chart_intake import prepare_backtest_chart_intake


ROOT = Path(__file__).resolve().parents[2]
MAIN_BASELINE_COMMIT = "986ebaaf964c19ebddd3efef468c3f252e08f5d7"
MAIN_PROTECTED_OBJECTS = dict([
  [
    "Research/data/backtest_equity_curve.png",
    "4d9e4831011c5790fd57b0fca775a34eed1dc314"
  ],
  [
    "Research/data/backtest_vs_journal.png",
    "ae6b2050903f20344b039506fb3cbe68f6ed9423"
  ],
  [
    "assets/2025-11-05_DOGEUSDT_S_entry.png",
    "09fc6fa2a5afac51ab443f042bf8f9f24e2e8e8d"
  ],
  [
    "assets/2026-04-01_BTC_S_context.jpg",
    "cbfcffa38b1592e20ef9cefb6fc0894b79dc14b0"
  ],
  [
    "assets/2026-04-01_BTC_S_entry.jpg",
    "9455d8c667cc7790f863a281460fbdef2dec63ad"
  ],
  [
    "assets/2026-04-01_BTC_S_tp1.jpg",
    "80ae8d32066eacc6e77bee6d6a3ac5bc0ac68be0"
  ],
  [
    "assets/2026-08-24_BTCUSDCP_L_vt-long_chart.jpg",
    "05ede38cd3e4b92a2c138397f740d550a9b04dd3"
  ],
  [
    "assets/2026-08-24_BTCUSDCP_L_vt-long_position.jpg",
    "f47dd8d3fd4304825f62b957672a88193bcb5177"
  ],
  [
    "assets/2026/05/Pasted image 20260609145000.png",
    "6fcc09658fc3381b0eaa954a0fd9554da313c9f3"
  ],
  [
    "assets/2026/05/Pasted image 20260609145158.png",
    "e383748b53a8f23a8f658d1b6142e7d6c4e16ba7"
  ],
  [
    "assets/2026/05/Pasted image 20260609150311.png",
    "8440dd7df50805175a7fcee16159b718229412f0"
  ],
  [
    "assets/2026/05/Pasted image 20260609150350.png",
    "8c50e15de00b459ce0d7d20da05d39b6adb5e3a5"
  ],
  [
    "assets/2026/06/Pasted image 20260609151205.png",
    "06d208ee7bd6dbab36c12014aa8a52e574fc57db"
  ],
  [
    "assets/2026/06/Pasted image 20260609151551.png",
    "d4fbb5240c1c36f8ddb87ac3461cb1169787df1a"
  ],
  [
    "assets/2026/06/Pasted image 20260609151826.png",
    "d4fbb5240c1c36f8ddb87ac3461cb1169787df1a"
  ],
  [
    "assets/2026/06/Pasted image 20260609151839.png",
    "3644352aebb1dc0d81210cd2c38d5d8123a0957b"
  ],
  [
    "assets/2026/06/Pasted image 20260609152058.png",
    "2db225444e9354dfb0a8453ef2fc0e26a39cc1e7"
  ],
  [
    "assets/2026/06/Pasted image 20260609152534.png",
    "c7bd5064187b2c2eaab870ce2265c391f306fbb5"
  ],
  [
    "assets/2026/06/Pasted image 20260609152750.png",
    "fd112d340be73b44e0e6f988523c1c76cd8e8350"
  ],
  [
    "assets/2026/06/Pasted image 20260609153245.png",
    "4351d02e61a04f89b29ea47410e94eeac2cc680b"
  ],
  [
    "assets/2026/06/Pasted image 20260609153423.png",
    "a3c6137299cf491b0714326db9c53cb74b4ea94b"
  ],
  [
    "assets/2026/06/Pasted image 20260609153536.png",
    "4186bed884cfe25ffb453f6e9ced318a879b40d2"
  ],
  [
    "assets/2026/06/Pasted image 20260609153832.png",
    "e17c6c160401903fe19aa7d4f6cf557815ffa0a5"
  ],
  [
    "assets/2026/06/Pasted image 20260609154133.png",
    "a01ede10a3dc254f0b199c4664bc503747baf8d5"
  ],
  [
    "assets/2026/06/Pasted image 20260609154511.png",
    "bcce699b83bd3e9848b13d9f8054d1ea7baa9983"
  ],
  [
    "assets/2026/06/Pasted image 20260609154850.png",
    "9a3a2ae36da768e497540281c406e6bbd17c87c2"
  ],
  [
    "assets/2026/06/Pasted image 20260630202822.png",
    "e61a0fa71c96c7eecea9d47802cabdffc836d7ed"
  ],
  [
    "assets/2026/07/Pasted image 20260709124451.png",
    "de2029321f7be3382786bdb3d83cec801ee5b82b"
  ],
  [
    "assets/2026/07/Pasted image 20260721114716.png",
    "8d6dc0597cf83c6a53dada12492073d7848f637c"
  ],
  [
    "assets/2026/07/Pasted image 20260721115002.png",
    "b1b9eb8a84ef1df95b46fcef91f93f3c70318c82"
  ],
  [
    "assets/2026/07/Pasted image 20260721125132.png",
    "28ab0fb7ee704f27ac1f39b8b1708d818515843b"
  ],
  [
    "assets/2026/07/Pasted image 20260721125520.png",
    "3354f15adeb5b125b8e1809008d287e46a4ed30f"
  ],
  [
    "assets/2026/07/Pasted image 20260721125708.png",
    "aae10e51d6e093c8caf72909fe073378610a0493"
  ],
  [
    "assets/2026/07/Pasted image 20260721130111.png",
    "ddf67368467f688475147ce8688c53f2c8373ffa"
  ],
  [
    "assets/2026/07/Pasted image 20260721130251.png",
    "87997d99a191a1647164c6c3d12aeb8722f58b14"
  ],
  [
    "assets/2026/07/Pasted image 20260721130451.png",
    "9078350e1716604715c8c7758255e775b4cca1ff"
  ],
  [
    "assets/2026/07/Pasted image 20260721130520.png",
    "9078350e1716604715c8c7758255e775b4cca1ff"
  ],
  [
    "assets/2026/07/Pasted image 20260721130802.png",
    "ac9a9566ecce716bfcd4a22a7660870892fb15b5"
  ],
  [
    "assets/2026/07/Pasted image 20260721131022.png",
    "7db12b9ed2e1849e708c1f72c58e3f4ee81b4a79"
  ],
  [
    "assets/2026/07/Pasted image 20260721131024.png",
    "7db12b9ed2e1849e708c1f72c58e3f4ee81b4a79"
  ],
  [
    "assets/2026/07/Pasted image 20260721131325.png",
    "bf85d817f7d05aab82f5bc4fd59c5d6aaee4476d"
  ],
  [
    "assets/2026/07/Pasted image 20260721131440.png",
    "38c8a3130725faa6ee8908394c56526b013693af"
  ],
  [
    "assets/2026/07/Pasted image 20260723234027.png",
    "d8565fbe2afba84ac26643380e1b032773f6e9f3"
  ],
  [
    "assets/2026/07/Pasted image 20260723234416.png",
    "92310ff3dd6dc9f611d4d29e1fbac9f8064ea796"
  ],
  [
    "assets/2026/07/Pasted image 20260723234544.png",
    "b902af5799062f78e6affc24a5359e7af0cb3dc6"
  ],
  [
    "assets/2026/07/Pasted image 20260723234849.png",
    "59fc04d9035f55f947ea76402c94a532e9e1fabc"
  ],
  [
    "assets/2026/07/Pasted image 20260723235133.png",
    "0ec5178ca6fcd71a48248f9e066d51228ece8ba1"
  ],
  [
    "assets/2026/07/Pasted image 20260723235420.png",
    "23a6942f3d84e51f2a57f65e2b54c63569dd611c"
  ],
  [
    "assets/2026/07/Pasted image 20260723235729.png",
    "703bb12ba5177cf72ced8dd3eb861ce785c67d39"
  ],
  [
    "assets/2026/07/Pasted image 20260723235818.png",
    "a4588d9edf106d7ca8a793e18e066ba19b2d6abe"
  ],
  [
    "assets/2026/07/Pasted image 20260724000207.png",
    "b8323e047125152245043332bc0cec6ade000a93"
  ],
  [
    "assets/2026/07/Pasted image 20260724000242.png",
    "2071d75c6f3e3726348f9106361f758a18b74be4"
  ],
  [
    "assets/2026/07/Pasted image 20260724000358.png",
    "1839cb05a67f564792e331d5f7b3c24ec202d5a1"
  ],
  [
    "assets/2026/07/Pasted image 20260724000727.png",
    "a891644fc81a38b612f9d424bf4b4265ce3cc31f"
  ],
  [
    "assets/2026/07/Pasted image 20260724000826.png",
    "da52f769d6a4d9d785407a3d5a7f2790020809a3"
  ],
  [
    "assets/2026/07/Pasted image 20260724000838.png",
    "91006b98c6079091943fed348c5636429246002a"
  ],
  [
    "assets/2026/07/Pasted image 20260724001042.png",
    "ed33351965165095bfe6ed4c8459807299195197"
  ],
  [
    "assets/2026/07/Pasted image 20260724001137.png",
    "b8565a74647a093098b331600dc46b17ef8f6e74"
  ],
  [
    "assets/2026/07/Pasted image 20260724001201.png",
    "36d1ebfdf10a6a5ad2eb468ff62436fc36952ed1"
  ],
  [
    "assets/2026/08/Pasted image 20260805143450.png",
    "42757d47203434883cfa0de012ad43a9fd92568c"
  ],
  [
    "assets/2026/08/Pasted image 20260805143610.png",
    "923d49f92234a2dc9c9aa54ac5c0054ad71c82ed"
  ],
  [
    "assets/2026/08/Pasted image 20260809174414.png",
    "92b0a7c8d6665c0f452651432d71bdcc3e4a7607"
  ],
  [
    "assets/2026/08/Pasted image 20260809174505.png",
    "bab13f648bfb17dc002bee6344e8020ff719e1d3"
  ],
  [
    "assets/2026/08/Pasted image 20260809175128.png",
    "85bd7cb6ec54763441c488e4cd9cf8d9a3862469"
  ],
  [
    "assets/2026/08/Pasted image 20260811152204.png",
    "7d75b9fa56f8344247e863126dfd648f0cb35c89"
  ],
  [
    "assets/2026/08/Pasted image 20260811152447.png",
    "e16060fa75f1e031410d9b51773f1a25c8551230"
  ],
  [
    "assets/2026/08/Pasted image 20260811153518.png",
    "acca090a6a4a3b08f5f8b665da790439648fba9b"
  ],
  [
    "assets/2026/08/Pasted image 20260811153655.png",
    "5f7943450a90c9c7b92b4c5e9af3992cc6b5267e"
  ],
  [
    "assets/2026/08/Pasted image 20260811153729.png",
    "f9416443ad68125399433502df57ff26fc71e03a"
  ],
  [
    "assets/2026/08/Pasted image 20260811154235.png",
    "07e978386802bb37b2f382386587ed91ec3b4284"
  ],
  [
    "assets/2026/08/Pasted image 20260811154357.png",
    "b5d6bcd446750d23b4517d7df30b88aff3f06c45"
  ],
  [
    "assets/2026/08/Pasted image 20260811205546.png",
    "be45e134c67b9bd7743d75860f8e7c8bb57a8259"
  ],
  [
    "assets/2026/08/Pasted image 20260811205647.png",
    "e49531dfe1395ac1e6753203b386b93c05f93e09"
  ],
  [
    "assets/2026/08/Pasted image 20260811205811.png",
    "8a546a1d6f04b2c2bbffdb54a588edd870625ec9"
  ],
  [
    "assets/2026/08/Pasted image 20260811210057.png",
    "0b26accadfc925abf6060296af1bad9b0d88760c"
  ],
  [
    "assets/2026/08/Pasted image 20260811210508.png",
    "eaed5680658e8e2be769e130e69bcc56c22209fa"
  ],
  [
    "assets/2026/08/Pasted image 20260811210813.png",
    "a96cc82c4e6c346526c686a709bccd48272ee341"
  ],
  [
    "assets/2026/08/Pasted image 20260811210951.png",
    "4f88017405fc0a6265d42454cece9054dd24e165"
  ],
  [
    "assets/2026/08/Pasted image 20260811211041.png",
    "c9611c67756d913dd35de31211652d88092e1b1f"
  ],
  [
    "assets/2026/08/Pasted image 20260811211144.png",
    "d91100f34d049ff8a5aa91727540d7e80cd6011b"
  ],
  [
    "assets/2026/08/Pasted image 20260811211412.png",
    "5888b76bbcacaa5fd32c25a858b33ef137dbaba2"
  ],
  [
    "assets/2026/08/Pasted image 20260811211448.png",
    "16540eac8bf66d3bf306fee9a31bc31e3415e3f0"
  ],
  [
    "assets/2026/08/Pasted image 20260811211856.png",
    "dd286d883cdbbcbf2f7939b426b936b9a91d3852"
  ],
  [
    "assets/2026/08/Pasted image 20260811212010.png",
    "58acbd71f1389fd9b3b97399850657b420ff3fd7"
  ],
  [
    "assets/2026/08/Pasted image 20260811212100.png",
    "f9ad2c582dd36c970880e77ba2a8ddef2cd110cf"
  ],
  [
    "assets/2026/08/Pasted image 20260811212418.png",
    "1e8fd0a4e065357cd5b4bc3e0031baa7fd8d6700"
  ],
  [
    "assets/2026/08/Pasted image 20260811212548.png",
    "37fc3683f0f85e5bd42a798a45ffd8b71761ad6c"
  ],
  [
    "assets/2026/08/Pasted image 20260811212641.png",
    "ee16a9ae2f1a9a46467cb0ff0cefec21881ea62e"
  ],
  [
    "assets/2026/08/Pasted image 20260811212733.png",
    "ee16a9ae2f1a9a46467cb0ff0cefec21881ea62e"
  ],
  [
    "assets/2026/08/Pasted image 20260811212742.png",
    "b360368c39193f57e47efb34de5e1f9fae1aa3b1"
  ],
  [
    "assets/2026/08/Pasted image 20260811213115.png",
    "26238f22b1d3c50895779386ac7c662eedef4579"
  ],
  [
    "assets/2026/08/Pasted image 20260811213208.png",
    "4aaa5de59ed091241919ba23bd27b11d8c2df753"
  ],
  [
    "assets/2026/08/Pasted image 20260811213541.png",
    "073e07f87d3384e5d9725aeffa12400604542df6"
  ],
  [
    "assets/2026/08/Pasted image 20260811213631.png",
    "1fafa6035885cbc88e424e435545cdbf0831f79d"
  ],
  [
    "assets/2026/08/Pasted image 20260811214237.png",
    "d46e292e0a1745b03db86484282a000b51c26b90"
  ],
  [
    "assets/2026/08/Pasted image 20260811214355.png",
    "928323bc266e49f221cfb1e51442b3d4f6a6af89"
  ],
  [
    "assets/2026/08/Pasted image 20260811214425.png",
    "d69582ffb726f091418c6fbcff64e5a6d814aa31"
  ],
  [
    "assets/2026/08/Pasted image 20260811215235.png",
    "55c8276c342b95f9aff86e4be85add9f26c70db6"
  ],
  [
    "assets/2026/08/Pasted image 20260811215323.png",
    "9920643faa3ae77d992cacfb040ad22c7950c077"
  ],
  [
    "assets/2026/08/Pasted image 20260811220128.png",
    "7d687411ef9c9f72bd569bcc430235100339000c"
  ],
  [
    "assets/2026/08/Pasted image 20260811220222.png",
    "02627b52703f3229ccef6a8f0fd951fa2935a74a"
  ],
  [
    "assets/2026/08/Pasted image 20260811220501.png",
    "934d5f5971bb1098748871721245af4b6c972fd1"
  ],
  [
    "assets/2026/08/Pasted image 20260811220553.png",
    "934d5f5971bb1098748871721245af4b6c972fd1"
  ],
  [
    "assets/2026/08/Pasted image 20260811220615.png",
    "af24fb50bf1205805468a7d827f4d33f727cfa20"
  ],
  [
    "assets/2026/08/Pasted image 20260811220910.png",
    "d3610cedb0e977192e6304b813422571601af281"
  ],
  [
    "assets/2026/08/Pasted image 20260811220933.png",
    "2326860b759c9e27113b6aedb82e067a958da0ca"
  ],
  [
    "assets/2026/08/Pasted image 20260811221051.png",
    "f95a97f3610f3d4190743fcd769b67fcecf5dea4"
  ],
  [
    "assets/2026/08/Pasted image 20260811221451.png",
    "a97e331a6151a1077d8240af4c60c7b59aae772e"
  ],
  [
    "assets/2026/08/Pasted image 20260811221536.png",
    "cfc8bbdf8bfefe5e2344940a077bb21cc58dfa6b"
  ],
  [
    "assets/2026/08/Pasted image 20260811221559.png",
    "656e9540bd66f7e0cfa25659da887af796957acb"
  ],
  [
    "assets/2026/08/Pasted image 20260811222151.png",
    "8c3ec41e5ea319c036b6ae29f7163b79ff695e82"
  ],
  [
    "assets/2026/08/Pasted image 20260811222308.png",
    "42747883dea53259998dfc409c4319b301e680cd"
  ],
  [
    "assets/2026/08/Pasted image 20260811222629.png",
    "766bbd067855d503296c3e62f6edd4b1278604c1"
  ],
  [
    "assets/2026/08/Pasted image 20260811222717.png",
    "766bbd067855d503296c3e62f6edd4b1278604c1"
  ],
  [
    "assets/2026/08/Pasted image 20260811223014.png",
    "20ab9bb0278d6f589754597a4a47941a52ca1b1c"
  ],
  [
    "assets/2026/08/Pasted image 20260811223042.png",
    "c9e2374ec1b1e09094232ac01d710f37817821ba"
  ],
  [
    "assets/2026/08/Pasted image 20260811223636.png",
    "2ef2272ec2bd4b9fcc34020bb740e9d52efa2ed4"
  ],
  [
    "assets/2026/08/Pasted image 20260811223709.png",
    "de2f66bac1c9a5675d32691da23261284ac90920"
  ],
  [
    "assets/2026/08/Pasted image 20260811224043.png",
    "bdfac5c6c2aebb5169ae4ddf46b27e4e6daac421"
  ],
  [
    "assets/2026/08/Pasted image 20260811224147.png",
    "105d7ecbd3f570bb90739069b5fb96340114a5f8"
  ],
  [
    "assets/2026/08/Pasted image 20260811224359.png",
    "389c5922a09e668a4c31ad539a7041795b272bed"
  ],
  [
    "assets/2026/08/Pasted image 20260811224517.png",
    "b1aa036056b832e5ba2c9de2080eef16eb36db53"
  ],
  [
    "assets/2026/08/Pasted image 20260811224531.png",
    "f48cdb3d1322439b64f56d04654ba5530f37ca30"
  ],
  [
    "assets/2026/08/Pasted image 20260811224657.png",
    "480ee716ebc32f419579a328f910446c756f36ef"
  ],
  [
    "assets/2026/08/Pasted image 20260811224755.png",
    "6a2927ee875ff2a1f6aca20f78295664e7038436"
  ],
  [
    "assets/2026/08/Pasted image 20260811224758.png",
    "6a2927ee875ff2a1f6aca20f78295664e7038436"
  ],
  [
    "assets/2026/08/Pasted image 20260811230746.png",
    "2be2855a90886e6effdf8df47cf0a90add920b0f"
  ],
  [
    "assets/2026/08/Pasted image 20260813002238.png",
    "beb43239081f140638096457589db2cdca7f110d"
  ],
  [
    "assets/Pasted image 20260813003229.png",
    "17d761be35f4167af36084c62ef2ac2649dac484"
  ],
  [
    "assets/Pasted image 20260813003315.png",
    "d4db505d0c7fcc80d6e58f04a30d62e7d9849c71"
  ],
  [
    "assets/Pasted image 20260813132341.png",
    "48b620be30301634d890e944f7b8d1453bfdcaf8"
  ],
  [
    "assets/Pasted image 20260813132833.png",
    "7cb115e084461e74f0dbd05e065d9e82b0d45eaf"
  ],
  [
    "assets/Pasted image 20260813133007.png",
    "5cf42020ee92439bed3ebf260e5e55c089aa169f"
  ],
  [
    "assets/Pasted image 20260813140758.png",
    "dd6b927a4a9e9ea163d4a324aef1fdd7fc6d670a"
  ],
  [
    "assets/Pasted image 20260814104249.png",
    "7eab554edf54fadf1c83ece42c4f60437062aed5"
  ],
  [
    "assets/Pasted image 20260814142900.png",
    "aee2a5443feaab50a4e1632419d7ac1cbee68bd4"
  ],
  [
    "assets/Pasted image 20260814142949.png",
    "1e7a9d30252bc135afdf459666bc3a18be7cafda"
  ],
  [
    "assets/Pasted image 20260814163332.png",
    "8dc1b589d8e9e4a34346aa9d98fc0743b1a3a487"
  ],
  [
    "assets/Pasted image 20260815130718.png",
    "1590db8d87bbec205e0b366ede8d4a3c7e822ff0"
  ],
  [
    "assets/Pasted image 20260817164054.png",
    "6339d6a4a8895cc2f5d6a1892f4d889dd19d7e3c"
  ],
  [
    "assets/Pasted image 20260817164235.png",
    "3f6050d94c3cfafb5edc93fe2737c1df3e0c25e1"
  ],
  [
    "assets/Pasted image 20260817164531.png",
    "626b2af8f6e8b5a81e0cd67c4ebc3705579491db"
  ],
  [
    "assets/Pasted image 20260818115536.png",
    "9221c6b7928e4e0fd3c48a27daf09d7cd97f5fa4"
  ],
  [
    "assets/Pasted image 20260818221758.png",
    "b5d38ba5ef3bdbb236bda3fc56e0e287aaa62c4e"
  ],
  [
    "assets/Pasted image 20260818223012.png",
    "17e3e09f6e1030c44131350c0436fc81a8faacac"
  ],
  [
    "assets/Pasted image 20260818223138.png",
    "1908fe0946bb6c027e37a2ba28f720e59bcf3dd7"
  ],
  [
    "assets/Pasted image 20260819112045.png",
    "3082c20118e8f41e5748e1eec757d38edc4ed4c9"
  ],
  [
    "assets/Pasted image 20260819113935.png",
    "1c84731cf3b2c7c6438ca9812a873db196b87806"
  ],
  [
    "assets/Pasted image 20260820153538.png",
    "81b3b5aa06bb63a3edbf23ce6f080f487b010921"
  ],
  [
    "assets/Pasted image 20260820153554.png",
    "24f54e2da1b0992b835d0e19ee4904e4d4814e66"
  ],
  [
    "assets/Pasted image 20260820153607.png",
    "76f15557a6353af350274acb2aaa04492e2aa816"
  ],
  [
    "assets/Pasted image 20260820153701.png",
    "8fe1a3438fafeb655e9182452d91382481ac84eb"
  ],
  [
    "assets/Pasted image 20260820153938.png",
    "db1a82ac3b241b0f50feb3146c9b42a91e415169"
  ],
  [
    "assets/Pasted image 20260820154247.png",
    "585d36e2621e2ce0dec0338ca31120721e48ff43"
  ],
  [
    "assets/Pasted image 20260820161934.png",
    "6a336bbc122f529fef8efc6a4dab7b23de0ed884"
  ],
  [
    "assets/Pasted image 20260824133435.png",
    "1f015dd13b113589cffd461b6f8b4f50a7018110"
  ],
  [
    "assets/Pasted image 20260824133445.png",
    "1f015dd13b113589cffd461b6f8b4f50a7018110"
  ],
  [
    "assets/Pasted image 20260824133452.png",
    "e67abd550b020bfdf63fc3b130ec07936e1c146d"
  ],
  [
    "assets/Pasted image 20260824133711.png",
    "273c102b4878a2eb77366deb5b80417f1da5527b"
  ],
  [
    "assets/Pasted image 20260824133804.png",
    "2f8440ed572e243dd7ccebaccc76284a0920ffa6"
  ],
  [
    "assets/Pasted image 20260824133843.png",
    "2452249966496a236625917fa53734a8e743d5ec"
  ],
  [
    "assets/Pasted image 20260825163647.png",
    "ddef8a6ff88810cadf32d42efdc83ff0a7ca1d08"
  ],
  [
    "assets/Pasted image 20260825163830.png",
    "b7ec5d921b8819337b02d815bbfe9a8e50b6480e"
  ],
  [
    "assets/Pasted image 20260825163837.png",
    "b7ec5d921b8819337b02d815bbfe9a8e50b6480e"
  ],
  [
    "assets/Pasted image 20260825163844.png",
    "b0c902d05edf3080917c608578f4d56c050e9e92"
  ],
  [
    "assets/setup_01.png",
    "4517135c9d7585dbae12849d8282e13c7286df01"
  ],
  [
    "assets/setup_02.png",
    "45175848dfb22f41b6975b654a9588243347dddd"
  ],
  [
    "assets/setup_03.png",
    "42b0a5bbc17f7949c403252be25dea451d5b3617"
  ],
  [
    "assets/setup_04.png",
    "6a6c4fc1a010b823add9597a138a9181a040f145"
  ],
  [
    "assets/setup_05.png",
    "199d6ae133a22f6b10f4e1f19ca3dd812ccad81b"
  ],
  [
    "assets/setup_06.png",
    "38ad6e78199ce108cbfa8a1d187b91caaa727015"
  ],
  [
    "assets/setup_08.png",
    "67915d20800e54b8c1f2e632c7da7898325097bd"
  ],
  [
    "assets/setup_09.png",
    "25cc538ac8fdd5a84da36251530f866d309d16fe"
  ],
  [
    "assets/setup_11.png",
    "c4b1fc1718e7e5a899e6f3239307904b28c0254e"
  ],
  [
    "assets/setup_12.png",
    "13e02f1c5542d5f6483127eb69c88e7993450cd8"
  ],
  [
    "canonical/evidence/artifacts/02b68ec6f96118f458edfd7e36dd76affdd36c566665a46ec8eb23f527505eb2.png",
    "7cbb4a84bcf90e2c2900876f718a2b77df1f91b8"
  ],
  [
    "canonical/evidence/artifacts/0387d7aac9c3c263771e1b67fdc98a7f82de28b7c3a3f1a28a49994aa4160de2.png",
    "2452249966496a236625917fa53734a8e743d5ec"
  ],
  [
    "canonical/evidence/artifacts/03e2cf5f570fc7f02e25e67cd158a429cdf375813e9914e8a99db291380f391a.png",
    "217926eeecf6fc955429e425bc44ca5e1eb897d6"
  ],
  [
    "canonical/evidence/artifacts/043a2419ed4421a78c638fa42521400bf4e8bf4343109370061f1ea4e17c0726.png",
    "c26b74e18e2d3cf7dd19a82c67ef2eaa6baca1b8"
  ],
  [
    "canonical/evidence/artifacts/06ed49a9f8c7b1e5d57848b9a0531cefef35596fe4c11d42492355a09bba79c1.png",
    "1dfc842a7c9578649e1afbe15d2bd1e93e4841de"
  ],
  [
    "canonical/evidence/artifacts/085b1e03f0b8d19a8292ca00a002a33f9bbd3ddc2053fad0beae67ba1a838e14.png",
    "1d1333d9e1e5c3d904ef310d17e7ffed743607c4"
  ],
  [
    "canonical/evidence/artifacts/0cbf3033413c6e04a9536a4db7e50bda8d3f9cf62c10800e5df6955c99544fdb.png",
    "4401eba436acbadee2ff7abeef2c5121564937f0"
  ],
  [
    "canonical/evidence/artifacts/11efee70d1ec4a5188df4b8f7081b38d1420e25f77aef346c6c03cb9aea703e5.png",
    "686573fa5fbb0053386da2fc9a7d2b9244692263"
  ],
  [
    "canonical/evidence/artifacts/1684366d1cab5ad5648d8dcd551ca754ca79ec3bfb0288cbefbb1454b72a8195.png",
    "17e6c98bbfd496e98cafb18d0df19443816dce8b"
  ],
  [
    "canonical/evidence/artifacts/1e9ab6275e62c528e0517c2e51a916cb0b99ba2782ab2e73975aa1e7153d8a72.png",
    "f6248a4ab6a6878976f81c5f4ca32c4ea3f4ecb1"
  ],
  [
    "canonical/evidence/artifacts/1f6a32565dbd326de66becf76da62f7ed66233b412eac385ecc7904de8aacca7.png",
    "fc25541de4823bfbdb76b4a748a2f47c2a1f4c34"
  ],
  [
    "canonical/evidence/artifacts/1fbb714e97576a98f54b7190790b392e941a1cb6ee3c7dd146acda8f38d65c3e.png",
    "6039c99553018ef1441b331151eb9e1063d2894c"
  ],
  [
    "canonical/evidence/artifacts/1feba295eb9544a6b6b2b6bcaa96a1a329d189115a2392cffb0940b7f944d8fa.png",
    "2bcad7a24064bf73a7ff2b202a00a4ec681a58ec"
  ],
  [
    "canonical/evidence/artifacts/27c93f72ddf5f80350e359c2323820b6bb51f7ed566d544de9dffe401626b9dd.png",
    "ca5371dd19d7b5620debb190de4604bfd3e21887"
  ],
  [
    "canonical/evidence/artifacts/27d97e160aa65a7aeac434d585157f361cee9dfdb912ab92a5b4f376d8572e80.png",
    "1304a0fbd4d069dacecd4a00e326347af9774458"
  ],
  [
    "canonical/evidence/artifacts/2d8ef4dd77de6f1cf327e156386986f9e1d0e2b0d73e2ed6b12728f695dfe5a1.png",
    "743e2f516c435e278e044f8ff257a625a9f4a958"
  ],
  [
    "canonical/evidence/artifacts/2fc6d6e6814f8c7f2c8be460873dc4f41c70da1d8bf6f8ce16255ae1543a4c6c.png",
    "b87b199784ed6a937ae6ff8b87db2c0c826c829a"
  ],
  [
    "canonical/evidence/artifacts/31c472a686b55fde578f47283aec2af3500552c71d383bd70407e7ffe5e33955.png",
    "feed6de7c89a613667a8883b35adbd28747f8c74"
  ],
  [
    "canonical/evidence/artifacts/34a8a8eaaa7999de88b099574cea6bb9d0d663f95e37b4143c89d200575c32c3.jpg",
    "05ede38cd3e4b92a2c138397f740d550a9b04dd3"
  ],
  [
    "canonical/evidence/artifacts/3a11f0ee9017f91a6b51a0327b30440477c71575be8bdfe52dd2cedceec69a93.png",
    "4466bef4e569cb2a73a07e57bf3c5fe7de995102"
  ],
  [
    "canonical/evidence/artifacts/3a3ef061a010e86409ef40b9033a8abbfd0eaab66e9e794b800a271125d92088.png",
    "62247b55230422532fccd267fce235469e05e117"
  ],
  [
    "canonical/evidence/artifacts/470d9bac8153c0915f2914baae2e16c0ff68d093078e0dd4269edf05aa9d7c84.png",
    "d508ae4666fd0be2668c950eb3d17592eb405f62"
  ],
  [
    "canonical/evidence/artifacts/4cbb4ceed69a6b8f4bc77bc5357becc8571d1837e36cdddf3774e6ad877cb72f.png",
    "077174e07859725afb39f2e91b20c25383373944"
  ],
  [
    "canonical/evidence/artifacts/4e643bf7eb36b54bcb4aadb49d96cd4c5ba53c2c018aa90b33a1a8326dc4afb9.png",
    "0ccb099bc050d36f5a419ca2d066de7a9917cadd"
  ],
  [
    "canonical/evidence/artifacts/5207df72551a8338d97808ae391509c68a36c01dc74c2c834440144051352453.png",
    "6476d331b336300a8b8d5eee0c3a860219f2e4f5"
  ],
  [
    "canonical/evidence/artifacts/5618fb374839358ca1351c3f9c85b992ac8c3fd5110bd242d7df47157efffad4.png",
    "73921b3145ebe124563ba9bf58c945892641cec9"
  ],
  [
    "canonical/evidence/artifacts/5f05f7c9e0a5881a769f29d3985241f5c1e385814111a3d78c1d7634bb54c0b0.png",
    "acfc03d9c1e58a587e7bbdb5924328247902e029"
  ],
  [
    "canonical/evidence/artifacts/604cabdaeefe4dcc72104d7ff47dc14c2be523d180570dd7a8befd419ca65644.png",
    "2d8e99c9b24bcdf0547db06d8d2b51668cb5bf49"
  ],
  [
    "canonical/evidence/artifacts/62afcacb4e6681afe6434504b4d11747c57c22dd0943a2d5eb055313cf3789f0.png",
    "2cbed24c6d0f8523ae0a2327ca57dd493a064f32"
  ],
  [
    "canonical/evidence/artifacts/64b621acb4dd018bf68fa31acf1e2add2f4939073e0af2fdb58f8785b4554267.png",
    "f519d5ed13ab24f3c429194fcdec289c8f3671c4"
  ],
  [
    "canonical/evidence/artifacts/6f126ef7d044a0b8c3b8ff5a5af761583f75d1638e158e6a18b17193d5921116.png",
    "2e1d0e43a9c54ae288d70f66f08f92c343ec73e8"
  ],
  [
    "canonical/evidence/artifacts/70098eaaaeece9ac329b9cedfc90060de879379cb2e6390b8fbd4394b7c0e785.png",
    "98c963db31ff05774a4de61c2ef6692c5c4fba78"
  ],
  [
    "canonical/evidence/artifacts/722fd231e42d05df45b1e7ed7026b272ba86741573299d816b73a0fe226c4d36.png",
    "a3710b835449abb5bed8b76ff5857a62415ffd54"
  ],
  [
    "canonical/evidence/artifacts/742cacfb310ce81e46a82328e55ec5b313dbc026e83bc6c08b3bdbcf3b950a98.png",
    "ca0d740ece98f0e6936411467c02c6453fc3060f"
  ],
  [
    "canonical/evidence/artifacts/744b4ed3b2b0e3e7d0a05a29c70ceea59f120a60bf0222f4fc637bca86486c68.png",
    "db322bf7876fedc2ab39aaed499a7fde0566e5f9"
  ],
  [
    "canonical/evidence/artifacts/767472c20133a39985e3abce92f90a041bca78206fa3f1d5f2533534e89a9ea1.png",
    "38b81a054a380e9525a468522aba2bebfc47b0b3"
  ],
  [
    "canonical/evidence/artifacts/76917dc049d2058dd861be487ac1fde6aaf317f0f76d7d10e9f5501a741f5c95.png",
    "5616b4cb5545e37beb63d4d15567e7f763a7c1c2"
  ],
  [
    "canonical/evidence/artifacts/7d64114934b2ada885b968c5904f3672a31b2e6d2ec5bf2763eba39c58900d68.png",
    "24558ae9ece30d7d2ab599f7736ff5da036b7441"
  ],
  [
    "canonical/evidence/artifacts/7dc824cb2e224062a4bb3ea6b6c7f0bb9a5498f4b4c28752816f571d1d439928.png",
    "6a752d243853a45337fa47367c8b9a2700a936c0"
  ],
  [
    "canonical/evidence/artifacts/8229aa972f48b9c877b77fbe8006d65a7c2f3e3302ebd667825a3c37649c01d1.png",
    "aacafbf6a9fdfbc1b8229ab17ff2c7526ca5635a"
  ],
  [
    "canonical/evidence/artifacts/864b41fb2ec231370a3772db823e5a3bf7431aa76ee18d90510649d746f9f160.png",
    "62557f69831e6c7492b7e77a63c454831ff34584"
  ],
  [
    "canonical/evidence/artifacts/8803bd9d1393342bf5d16298f8f417fbf7e0b782eac8e7f2cd114e11897ddb90.png",
    "72e1bba371da8153dfa7bbebcf1300105aba2c7b"
  ],
  [
    "canonical/evidence/artifacts/8b09852de48be376535f5a680c0e07607ba7b762aba26365a8f7bccdbfd70954.png",
    "ee009ca6215c465710e955e3c4affff996090061"
  ],
  [
    "canonical/evidence/artifacts/8c801c4abe22a870370589153f61709f1d45b260715edceca0cea902a63fa16e.png",
    "d354c892a792347cdb58ab622db9c01920e6f1ef"
  ],
  [
    "canonical/evidence/artifacts/8fd7bead0a2766525fe018f51646b5ef051f11b8d1dcfd0151295e2959d51d81.png",
    "b137aaa9079d78a8164cc7b9bc8618102e7ff420"
  ],
  [
    "canonical/evidence/artifacts/90501071dcac6132082e16fceda4efee5dfd0527dc0a7cd27d237f872abdd824.png",
    "470157c807879df6f7d194aa64dfc4ed62aa120d"
  ],
  [
    "canonical/evidence/artifacts/953e88205d957f486cb6c00cdae649643a227b860530f81ab1ba50a4622891fc.png",
    "846696959a8cca0d28e85de593776702ade935f7"
  ],
  [
    "canonical/evidence/artifacts/95601a1522a5523370538d971b73ecbe2727b9ec7e220d86c19c92bb3b6560a2.png",
    "8053133edc99d9f167a288b12c7c82f43f0a794c"
  ],
  [
    "canonical/evidence/artifacts/97f929548cec35222871e2325cd5f7ba78100b6edd052375af2077ee56050fc4.png",
    "c09a604cd65d0bf3302504f46bef095e994551ff"
  ],
  [
    "canonical/evidence/artifacts/9aceaa3ba4e6c271a3a6973ca3f7d00aaf8fde79ac2a8a2ca3018f369ecf2d00.png",
    "345e0d98a200731d1636fbc5c4afff525f669cc5"
  ],
  [
    "canonical/evidence/artifacts/9ae47286166befbd4d63328e12f490e400f9f2df47d5f4786ae2d34025368345.png",
    "b0c69c50c4dc6e23381c09b740b77f38a60efba4"
  ],
  [
    "canonical/evidence/artifacts/9cf1e061058e1cce7791eafc48557348bf74a7ae2993243db697bc109f99585d.png",
    "8d9ace6cbdc744c647613f9213d50d1df10fe8a1"
  ],
  [
    "canonical/evidence/artifacts/a47b0854442e5d0409386e23ff7bd59590e1a1c057eaddeabfb2212f4e88c955.png",
    "cf544c8ff760fb48e189aab5f8d673a77de26cbf"
  ],
  [
    "canonical/evidence/artifacts/a5e25c70b25ab141b574d3c061f4a9eb2ffb18bbdc214b0e7a9b3899cad22461.png",
    "2997b3946f40585ef7bd4bbb4b2aaae345192ffd"
  ],
  [
    "canonical/evidence/artifacts/a7b9fdab8d99efa8e2672f8389b4cf2832136aa01118cb6f360985f10d30b543.png",
    "0c7d4e21c11c289cd7eba4284a1e5b7094e2647b"
  ],
  [
    "canonical/evidence/artifacts/a8a2d9b21816907a83547a7f1f27783f085c7fff81895449ab7ebbe5a809b022.png",
    "fb52c494f864ea674c9af45a2bfe37aa3241f56a"
  ],
  [
    "canonical/evidence/artifacts/acb0fbe0cf77b3f80ec9e3f6596fd35afc7b19ebb10a36fd12163f9fc9efcaa0.png",
    "c91d7d6b8d9d4111c0399ddc63ae78004754f2a8"
  ],
  [
    "canonical/evidence/artifacts/aee7d367d1da8e5b4973e25cf6dbcc0a02850cdfb49b0f9be3a826efd5de478a.png",
    "a60ffd432d4b3fefa061c7c15a46417c4255d9ce"
  ],
  [
    "canonical/evidence/artifacts/b82fa8eae16c8584cbcba587fbe7927aa45ded7733c66306754faa535cb31651.png",
    "3fe079f5482b8420eb1dfcfd1625ba3a8471894f"
  ],
  [
    "canonical/evidence/artifacts/bd4a89763bc059a2f6737d71659eb2e4545216ad59f7720e5219c18edad836c9.png",
    "f186d3c57bc0e9402033444cbc1bd2b312f7d786"
  ],
  [
    "canonical/evidence/artifacts/cb53fc5b7946c6bb19231cadf7d8a28fed002d09be7d2974951e921f8fd88f15.png",
    "1952cb48443c5b9df0e095af5f3cb178aadf2c3d"
  ],
  [
    "canonical/evidence/artifacts/cf784efac3d9a74202fba7ca11428a202bf90cc9bb2947d46f9e98f67b36bedc.png",
    "79d3b21fc4b182e495fb5bd51751be7dd7ec2cb9"
  ],
  [
    "canonical/evidence/artifacts/dfcafe619d4d64fe7d0fcdb47bba9fce3feeb214f86eea33470e65b49a736e46.png",
    "7a68b37a694bf7e86b03526789e4ad89c45a8fa9"
  ],
  [
    "canonical/evidence/artifacts/e6f9d444f78d0c9e1382f260d585fd3eab61e731322e61596ca3d23935e72c76.png",
    "d69f8961eedac279c59ac6b98b13b693b84e607c"
  ],
  [
    "canonical/evidence/artifacts/ea553253fbcbde32ed9a3d12324e8f646835924ebe8c56dc865458f31185baa4.png",
    "01c6ebd3649c7cda4ff57fb0247718f4e4d50ec0"
  ],
  [
    "canonical/evidence/artifacts/f74d9e67b08de09adb818348facac74d3f602ed29fda99241a8df907a6c670d0.png",
    "ca0c145afd3a5a177db500c26aa80a50010baf45"
  ],
  [
    "canonical/evidence/artifacts/fe0f4ebf0d47585c3eaed898d527fb232cdc17b405292f773abe8b13ccfa5f30.png",
    "cbf502f62c0b852fe095923a04dd40453c6f3276"
  ],
  [
    "canonical/records/trades/trd_01a0609c-e613-7ee7-b7e7-33a9516c6e47.json",
    "bc3c8df1a2a9734151d77ee30acd2397e5216398"
  ],
  [
    "canonical/records/trades/trd_01a0a3c3-69d8-7a39-bb69-f90cd3664416.json",
    "b0542f5b5468a746808bbdf0d9a4231daec1935e"
  ],
  [
    "canonical/records/trades/trd_01a0a4e8-96b1-7348-b4e8-a9cdcbccb443.json",
    "7f5d8f0466c8fb29c7b9f9cac76e86e1d909ec47"
  ],
  [
    "canonical/records/trades/trd_01a0a4e8-9a93-774c-bde5-0760505595dc.json",
    "a65b1f933745a8656e367d02afaf8279761b4ea8"
  ],
  [
    "canonical/records/trades/trd_01a0a4e8-9e95-784e-bdd2-fdfb9e389c2b.json",
    "cd568b788831e3c845f566a9b9dd15a83805b726"
  ],
  [
    "canonical/records/trades/trd_01a0c336-98f1-7f30-9235-40d0521ff974.json",
    "7b75fd4b47c53151c0b812431110d6db09b7135e"
  ],
  [
    "canonical/records/trades/trd_01a0c4d5-ea2f-736d-a375-552bc1ec50e7.json",
    "ae05f0275d4c24a6aff76f796260e7ad54035296"
  ],
  [
    "canonical/records/trades/trd_01a0c72e-e4a8-7672-8d65-8dc1f443f640.json",
    "2f6d0122ce931e4db94a2b47a59c8fadc119f2ec"
  ],
  [
    "canonical/records/trades/trd_01a0c83e-1a97-7d20-961d-77cc5c867a58.json",
    "77fdb8d018672204da78e64df488bccb0a72a7b3"
  ],
  [
    "canonical/records/trades/trd_01a0c83e-1c91-7f39-b011-e679a3b9104e.json",
    "3a5143e3ba90f94b404bdad58a4f22519f5486f4"
  ],
  [
    "canonical/records/trades/trd_01a0c84c-35ae-7088-aaf6-8a53570d3e13.json",
    "8fa2c063a4b49f3088e7fef59e8572256b58e2a9"
  ],
  [
    "canonical/records/trades/trd_01a0c84c-3883-79c4-8214-b930fb0f8609.json",
    "886190d4337cda8dc4d4d1cb4f54eeec08e15fe6"
  ],
  [
    "canonical/records/trades/trd_01a0c84c-3b33-7f9e-add9-1caad2bdbf36.json",
    "0abc10e034edd750e939c056fa8a2daa53501ca2"
  ],
  [
    "canonical/records/trades/trd_01a0ca11-f20a-7dc4-b969-5093a3db9961.json",
    "8c65da6c07f54faca532d4c4d542e1190cdcc011"
  ],
  [
    "canonical/records/trades/trd_01a0ca11-f633-7daf-aae1-a0e73036a87a.json",
    "ef2b1fca8a2a6bc5ac07b08f801e4e68772b2ffb"
  ],
  [
    "canonical/records/trades/trd_01a0ca11-fa30-7ba5-8127-ca8b1097e541.json",
    "77bd334f9dc9be2d82409e8fba17286b6a10b4a7"
  ],
  [
    "canonical/records/trades/trd_01a0ca11-fe10-7db7-aa50-7fa5e42e9288.json",
    "75c02c0885ca8699bb2abef75a3f0ad2b0928737"
  ],
  [
    "canonical/records/trades/trd_01a0ca12-01ad-724e-bc0c-41a8efaade97.json",
    "78230a7937731273d14a4b87e2ec141bd7b78047"
  ],
  [
    "canonical/records/trades/trd_01a0ccd8-b17a-75be-8f61-e35b30bb41dc.json",
    "9aa17fde760654112eb28b31549d06509d28934a"
  ],
  [
    "canonical/records/trades/trd_01a0ccdf-db76-7a3b-97e9-8df5cc266545.json",
    "afdf24b37368b122ad316111a26b7e6c8a3c6115"
  ],
  [
    "canonical/records/trades/trd_01a0ccdf-ed55-7fa5-a413-a2aa20d6c0f9.json",
    "85497c8b18d0df763b65dc7bd05350bd73a0e4f4"
  ],
  [
    "canonical/records/trades/trd_01a0ccdf-fec2-785f-a63e-02bc5e728ae9.json",
    "32af8226fa16f6ad57990073aa0745fecdd632ec"
  ],
  [
    "canonical/records/trades/trd_01a0cce0-1026-72c2-adf6-3e45eb669498.json",
    "8194b7aacb87cdd188f638d8ab2537b21ed8a254"
  ],
  [
    "canonical/records/trades/trd_01a0cce0-1da8-759d-830f-9ce8ca9ac957.json",
    "0ed2eb9eb44e7a50cddd9b3ecef70ebd263e7d30"
  ],
  [
    "canonical/records/trades/trd_01a1173e-0acf-74da-8812-2e3642b1c09f.json",
    "0bb8e3b9d7860eaa72be61425813774bb206b5e2"
  ],
  [
    "canonical/records/trades/trd_01a1173e-1201-78f3-9898-37bd37c6e46e.json",
    "84c1614f5fbf20eae55d4a4d74aa1ff214e89100"
  ],
  [
    "canonical/records/trades/trd_01a11911-98da-70a1-9f8b-3d05aed89f91.json",
    "b7b41378adc4d7f8fca9364e2637d0fff6465a31"
  ],
  [
    "canonical/records/trades/trd_01a119c6-981a-7ae3-9b10-d84f7c035548.json",
    "877d3d61c451ea74545cfa0a8ae3c88690fb92c9"
  ],
  [
    "canonical/records/trades/trd_01a11a9f-dea0-729d-8ca4-5375ad37d050.json",
    "e6d4f196a64cee753b7291fbccc87085db8bfaf2"
  ],
  [
    "canonical/records/trades/trd_01a11b4d-689d-7f6e-ad81-ad65d8a664bb.json",
    "5e88bade40696259de48e200b237656e3419306b"
  ],
  [
    "canonical/records/trades/trd_01a11c26-f252-7ba5-8f3f-53af6bca21d7.json",
    "64b0c21f2a652dc2918b4cdc16eb0fb1d0a08d1d"
  ],
  [
    "canonical/records/trades/trd_01a11cdf-d73b-7f57-a807-5b711e5ba3ff.json",
    "75d7c18ba05e84b7c545fc499805884556c96e82"
  ],
  [
    "canonical/records/trades/trd_01a1261c-46c1-7292-9f75-138f00bd527f.json",
    "b8c32c28b4aeaf1ae6a3857b5ba8e915c9c8cfcb"
  ],
  [
    "ingestion/pilot/batch-2/charts/02-entry.png",
    "1730648e1f866d775c2feefbb8deb3a05a7f7750"
  ],
  [
    "ingestion/pilot/batch-2/charts/02-exit.png",
    "69a5808906af763df6370ac0cf5488b8dbe4147f"
  ],
  [
    "ingestion/pilot/batch-2/charts/06-entry.png",
    "d5d403bed1c3dbc9b713d875bdefe42fd0e2821e"
  ],
  [
    "ingestion/pilot/batch-2/charts/06-exit.png",
    "4ace94060225929dd2cade9577b62750efb6385a"
  ],
  [
    "ingestion/pilot/batch-2/charts/06-orig-entry.png",
    "17d97847e815d91b4f718944d1d1c5cda7b45270"
  ],
  [
    "ingestion/pilot/batch-2/charts/06-orig-exit.png",
    "1979017a2ca436e4c94ca094318cc45d2ab91a65"
  ],
  [
    "ingestion/pilot/batch-2/charts/09-entry.png",
    "819e225a6ae1528e16a1ab87e66fafef729ce301"
  ],
  [
    "ingestion/pilot/batch-2/charts/10-entry.png",
    "fe221129ba806ba74ac83f3bed1509ab11dc5065"
  ],
  [
    "ingestion/pilot/batch-2/charts/13-entry.png",
    "b79eaad745e6f0a27791c1784f83dd4e6e765b52"
  ],
  [
    "ingestion/pilot/batch-2/charts/13-exit.png",
    "25f045268afe1d0445a5001c37933d281aed97cd"
  ],
  [
    "ingestion/pilot/batch-2/charts/13-orig-entry.png",
    "4d282d108cbc5d535215608e1076a72e29fe0e8b"
  ],
  [
    "ingestion/pilot/batch-2/charts/13x-exit.png",
    "80215d46cd456a65a9a315a31bfbd096bd0b602e"
  ],
  [
    "trades/2026/04/image-1.png",
    "7e99b1be2a46ca8fe0f3196f145368767758ad63"
  ],
  [
    "trades/2026/04/image-2.png",
    "77f79c8a8629d9ce67f36a216589a8e6e4116f2c"
  ],
  [
    "trades/2026/04/image-3.png",
    "270c38e041b57a4ea978dfac0ef53235e9f69909"
  ],
  [
    "trades/2026/04/image-4.png",
    "ed6aa5e2512e64ef08b68dd7d1ed91ee7cfa9c5a"
  ],
  [
    "trades/2026/04/image-5.png",
    "8b1bbc6d98d38291558632441e4b76e011b41b89"
  ],
  [
    "trades/2026/04/image-6.png",
    "1bd00f34b8f8d5318f65d7f32d6c2847ba883d83"
  ],
  [
    "trades/2026/04/image-7.png",
    "55b90b3361b406bbb7470aa5f66af7231e848d90"
  ],
  [
    "trades/2026/04/image-8.png",
    "baf1b62efb347122b73c9a62f07b35874ecb8afe"
  ],
  [
    "trades/2026/04/image.png",
    "d58f9901f290c8a5ead00e8c9f37d347cf46be83"
  ],
  [
    "trades/2026/05/image.png",
    "b86bd9ec5a64d00073d8f6861a43af4948ce3605"
  ]
])
LIVE_NUMBER = "T000033"
REVIEW_NUMBER = "T000025"
EXPECTED_BACKTEST_NUMBER = "BT000001"


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def _json(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _projection_records():
    return list(_json("canonical/lifecycle/current.json")["records"].values())


def test_original_main_33_trades_and_264_images_are_exact_byte_identical():
    assert len([p for p in MAIN_PROTECTED_OBJECTS if p.startswith("canonical/records/trades/")]) == 33
    assert len([p for p in MAIN_PROTECTED_OBJECTS if not p.startswith("canonical/records/trades/")]) == 264
    from trading_os.legacy_source import LegacySourceError, read_legacy_bytes
    for path, baseline_blob in MAIN_PROTECTED_OBJECTS.items():
        # Legacy originals were retired into the EvidenceStore (byte-exact,
        # ledger-verified); every other object must still be on disk.
        try:
            data = read_legacy_bytes(ROOT, path)
        except LegacySourceError:
            raise AssertionError(f"Original main data missing: {path}")
        assert _git_blob_sha(data) == baseline_blob, f"Original main data modified: {path}"


def test_aa_two_original_png_blobs_template_and_no_fabricated_execution():
    case = "Research/backtest-candidates/nas100-issue-144"
    packet = _json(f"{case}/agent-intake.v1.json")
    assert packet["strategy"]["strategy_id"] == "stg-example-a"
    assert packet["strategy"]["rule_version"] == "stg-example-a:v1"
    assert packet["mode"] == "BACKTEST"
    assert packet["owner_plan_variant"] == "A"
    images = {x["role"]: x for x in packet["images"]}
    assert set(images) == {"entry_chart", "exit_chart"}
    assert images["entry_chart"]["git_blob_sha"] == "3a94ae3dffa5842ae60b42124cbfcfe9b2bf2114"
    assert images["exit_chart"]["git_blob_sha"] == "9c01c31da91959e90738ae6051589813cfa5fba0"
    for item in images.values():
        image = ROOT / item["ref"]
        assert image.is_file()
        assert _git_blob_sha(image.read_bytes()) == item["git_blob_sha"]
    preview = prepare_backtest_chart_intake(packet)
    assert preview["planned_rr_from_chart"] == "3.348"
    assert preview["price_quality"] == "chart_derived"
    assert preview["simulated_entry_at"] is None
    assert preview["actual_exit_verified"] is False
    assert preview["performance_counted"] is False
    assert preview["canonical_bt_id"] is None


def test_existing_live_provider_facts_and_no_second_registration():
    rows = _projection_records()
    candidates = [x for x in rows if x.get("provider", {}).get("provider_lifecycle_id") == "2506100706543892"]
    assert len(candidates) == 1, "Duplicate registration for the same provider lifecycle"
    record = candidates[0]
    assert record["trade_no"] == LIVE_NUMBER
    source = _json("canonical/evidence/artifacts/03741ea04c60a0435239ee03f040765f53725747c9e11ce53d911d2be5bbfcb9.jsonl")
    provider = source["lifecycle"]
    assert provider["open_id"] == "2506100706543892"
    assert float(provider["entry_price"]) == record["entry"]["actual_entry"]
    assert provider["entry_time"] == record["entry"]["execution_at"]
    assert float(provider["weighted_exit_price"]) == record["close"]["exit_price"]
    assert provider["exit_time"] == record["close"]["closed_at"]
    assert float(provider["net_profit_sum"]) == record["provider_pnl"]
    assert record["execution_verification"]["entry"]["basis"] == "external_fill"
    assert record["exit_verification"]["state"] == "confirmed"
    assert _json("canonical/sequences/numbers.json")["allocated"].count(LIVE_NUMBER) == 1


def test_t000025_review_is_projected_without_rewriting_original_trade():
    rows = [x for x in _projection_records() if x.get("trade_no") == REVIEW_NUMBER]
    assert len(rows) == 1
    value = rows[0]
    assert value["state"] == "REVIEWED"
    assert value["strategy_id"] == "stg-ict-liquidity"
    assert value["rule_version"] == "stg-ict-liquidity:v1"
    assert value["review"]["rule_compliance"] == "compliant"
    assert value["review"]["diagnostic_class"] == "INSUFFICIENT"
    assert "114.983" in value["review"]["owner_notes"]
    original = _json("canonical/records/trades/trd_01a1173e-0acf-74da-8812-2e3642b1c09f.json")
    assert original["trade_no"] == REVIEW_NUMBER
    assert original["review"]["status"] == "not_started", "Original trade was overwritten to save review"


def test_release_gate_real_aa_canonical_bt_and_readback_not_merely_a_template():
    """Expected RED until a genuine external Fresh Agent has written & reread main.

    This guard deliberately cannot be satisfied by a draft PR or claimed BT ID
    in research candidates. Success also needs separate external Agent trace.
    """
    records = _projection_records()
    matches = [x for x in records if x.get("trade_no") == EXPECTED_BACKTEST_NUMBER]
    assert len(matches) == 1, (
        "ACCEPTANCE FAIL: AA has no canonical BT000001 in main lifecycle. "
        "A PR/template/png does not count as registration."
    )
    value = matches[0]
    assert value["trade_type"] == "backtest"
    assert value["strategy_id"] == "stg-example-a"
    # Registered as v1 on 2026-10-12; the Owner re-pinned it to v2 on 2026-11-22 (append-only amendment).
    assert value["rule_version"] in {"stg-example-a:v1", "stg-example-a:v2"}
    assert any(e.get("sha256") for e in value.get("evidence", []) if e.get("type") == "replay_chart")
    assert value.get("source_method") == "tradingview_replay"

def test_aa_backtest_is_visible_in_generated_trade_view_without_claiming_provider_profit():
    view = _json("exports/operational/trade-view/trade-view-data.json")
    serialized = json.dumps(view, ensure_ascii=False)
    assert "BT000001" in serialized
    assert "stg-example-a" in serialized
    aa = [row for row in _projection_records() if row.get("trade_no") == "BT000001"]
    assert len(aa) == 1
    assert aa[0].get("provider_pnl") is None
    # Since 2026-11-22 the Owner-confirmed chart exit gives a realized R; it is never a provider fill.
    assert (aa[0].get("exit_verification") or {}).get("basis") in {None, "owner_attestation"}
    assert aa[0]["entry"]["execution_at"] is None
    assert aa[0].get("review_status") == "not_started"
