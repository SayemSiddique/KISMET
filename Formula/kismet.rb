class Kismet < Formula
  desc "Autonomous CLI agent for AI-guided image harvesting with local LLM planning"
  homepage "https://github.com/SayemSiddique/kismet"
  url "https://github.com/SayemSiddique/kismet/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"

  depends_on "python@3.12"

  def install
    python3 = Formula["python@3.12"].opt_bin/"python3"
    system python3, "-m", "pip", "install", "--prefix=#{prefix}", "--no-deps", "."
    bin.install "bin/kismet"
  end

  test do
    assert_match "kismet", shell_output("#{bin}/kismet --help")
  end
end
