package io.github.ayamedog.fgostats.devtools;

import com.android.tools.smali.dexlib2.DexFileFactory;
import com.android.tools.smali.dexlib2.Opcodes;
import com.android.tools.smali.dexlib2.iface.ClassDef;
import com.android.tools.smali.dexlib2.iface.DexFile;
import com.android.tools.smali.dexlib2.iface.MethodImplementation;
import com.android.tools.smali.dexlib2.iface.MethodParameter;
import com.android.tools.smali.dexlib2.iface.debug.DebugItem;
import com.android.tools.smali.dexlib2.rewriter.ClassDefRewriter;
import com.android.tools.smali.dexlib2.rewriter.DexRewriter;
import com.android.tools.smali.dexlib2.rewriter.MethodImplementationRewriter;
import com.android.tools.smali.dexlib2.rewriter.MethodParameterRewriter;
import com.android.tools.smali.dexlib2.rewriter.Rewriter;
import com.android.tools.smali.dexlib2.rewriter.RewriterModule;
import com.android.tools.smali.dexlib2.rewriter.Rewriters;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Collections;

/** 开发工具：按 DEX 结构重写，移除调试信息；从不打入安装包。 */
public final class StripDexDebug {
    public static void main(String[] args) throws Exception {
        if (args.length != 3) throw new IllegalArgumentException("INPUT_INVALID");
        Path root = Path.of(args[0]).toRealPath();
        Path source = Path.of(args[1]).toRealPath();
        Path destination = Path.of(args[2]).toAbsolutePath().normalize();
        if (!source.startsWith(root) || !destination.getParent().toRealPath().startsWith(root)
                || Files.exists(destination) || Files.isSymbolicLink(source)) {
            throw new IllegalArgumentException("INPUT_INVALID");
        }
        DexFile original = DexFileFactory.loadDexFile(source.toFile(), Opcodes.forApi(24));
        DexRewriter rw = new DexRewriter(new RewriterModule() {
            @Override public Rewriter<ClassDef> getClassDefRewriter(Rewriters rewriters) {
                return new ClassDefRewriter(rewriters) {
                    @Override public ClassDef rewrite(ClassDef value) {
                        return new RewrittenClassDef(value) {
                            @Override public String getSourceFile() { return null; }
                        };
                    }
                };
            }
            @Override public Rewriter<MethodImplementation> getMethodImplementationRewriter(
                    Rewriters rewriters) {
                return new MethodImplementationRewriter(rewriters) {
                    @Override public MethodImplementation rewrite(MethodImplementation value) {
                        return new RewrittenMethodImplementation(value) {
                            @Override public Iterable<? extends DebugItem> getDebugItems() {
                                return Collections.emptyList();
                            }
                        };
                    }
                };
            }
            @Override public Rewriter<MethodParameter> getMethodParameterRewriter(Rewriters rewriters) {
                return new MethodParameterRewriter(rewriters) {
                    @Override public MethodParameter rewrite(MethodParameter value) {
                        return new RewrittenMethodParameter(value) {
                            @Override public String getName() { return null; }
                        };
                    }
                };
            }
        });
        DexFileFactory.writeDexFile(destination.toString(), rw.getDexFileRewriter().rewrite(original));
        System.out.println("DEX_STRUCTURAL_REWRITE_DONE");
    }
}
